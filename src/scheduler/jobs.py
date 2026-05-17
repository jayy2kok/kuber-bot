"""
Scheduled Jobs — APScheduler job definitions.

Reference: Plan.md P6.1

Schedule:
  Full Scan:       Daily 6:00 AM IST
  Daily Digest:    Daily 9:00 AM IST (top 10 buy + all sell)
  Bulk Deal Check: Daily 4:00 PM IST
  News Scan:       Every 30 mins
  Portfolio Sync:  Daily 9:00 AM IST
"""

import logging
from datetime import datetime

from src.config import get_settings
from src.data.yahoo_client import fetch_and_store_stock_data
from src.data.nse_client import fetch_and_store_deals, backfill_deals
from src.data.news_client import fetch_and_store_news
from src.data.stock_universe import get_all_active_stocks, get_industry_pe_median
from src.engine.recommender import run_full_scan
from src.scheduler.cleanup import job_data_cleanup

logger = logging.getLogger(__name__)
settings = get_settings()


def _last_expected_trading_day() -> 'date':
    """
    Estimate the last day Yahoo Finance would have price data for.

    Accounts for:
      - Time of day: before 4 PM IST, don't expect today's data
      - Weekends: walk back to last weekday
      - NSE holidays: skip known market holidays
    """
    from datetime import date, timedelta
    from src.data.nse_holidays import is_nse_holiday

    today = date.today()

    # Before market close (~4 PM IST), we don't expect today's data
    if datetime.now().hour < 16:
        today -= timedelta(days=1)

    # Skip weekends AND NSE holidays backward
    while is_nse_holiday(today):
        today -= timedelta(days=1)

    return today


async def _get_smart_period() -> str | None:
    """
    Determine the optimal yfinance period based on the latest price date in the DB.

    Returns a yfinance period string, or None if data is already current.
    """
    from src.db.engine import get_session
    from src.db.models import StockPrice
    from sqlalchemy import select, func
    from datetime import date

    async with get_session() as session:
        latest_date = await session.scalar(
            select(func.max(StockPrice.date))
        )

    if latest_date is None:
        logger.info("No price data found — using 2y for initial fetch")
        return "2y"

    expected = _last_expected_trading_day()
    gap_days = (expected - latest_date).days

    logger.info(
        f"Latest price: {latest_date}, expected trading day: {expected}, gap: {gap_days}d"
    )

    if gap_days <= 0:
        # DB data is at or ahead of expected trading day — skip fetch
        logger.info("Data is up-to-date — skipping fetch")
        return None
    elif gap_days <= 5:
        period = f"{gap_days + 2}d"  # +2 buffer for weekends/holidays
    elif gap_days <= 30:
        period = "1mo"
    elif gap_days <= 90:
        period = "3mo"
    elif gap_days <= 365:
        period = "1y"
    else:
        period = "2y"

    logger.info(f"Will fetch period={period}")
    return period


async def job_full_scan(period: str | None = None):
    """
    Daily scan job — fetch latest data and run recommendation engine.

    Args:
        period: yfinance period to fetch. If None, auto-detects based on
                the latest price date in the DB. If auto-detect finds
                data is already current, fetch is skipped entirely.
    """
    # Auto-detect unless explicitly provided (e.g. /fetch passes "2y")
    skip_fetch = False
    if period is None:
        period = await _get_smart_period()
        if period is None:
            skip_fetch = True

    logger.info(f"🔄 Starting full scan job (period={period}, skip_fetch={skip_fetch})...")
    start = datetime.now()

    if not skip_fetch:
        # 1. Refresh stock data (OHLCV + fundamentals)
        stocks = await get_all_active_stocks()
        success, fail = 0, 0

        for stock in stocks:
            industry_pe = get_industry_pe_median(stock.industry)
            ok = await fetch_and_store_stock_data(
                symbol=stock.symbol,
                stock_id=stock.id,
                industry_pe_median=industry_pe,
                period=period,
                delay=0.1,
            )
            if ok:
                success += 1
            else:
                fail += 1

        logger.info(f"Data refresh: {success} ok, {fail} failed")
    else:
        logger.info("📦 Data already current — skipping fetch, running analysis only")

    # 2. Run recommendation engine
    recommendations = await run_full_scan()
    elapsed = (datetime.now() - start).total_seconds()

    logger.info(
        f"✅ Full scan complete in {elapsed:.0f}s — "
        f"{len(recommendations)} recommendations to deliver"
    )
    return recommendations


async def job_bulk_deal_check(backfill: bool = False):
    """
    Daily 4:00 PM IST — Fetch bulk/block deals from NSE.

    On first run (backfill=True), fetches 30 days of historical data.
    """
    logger.info("🏦 Fetching bulk/block deals...")
    if backfill:
        count = await backfill_deals(days=30)
    else:
        count = await fetch_and_store_deals()
    logger.info(f"✅ Stored {count} institutional deals")


async def job_news_scan():
    """Every 30 mins — Fetch latest news from RSS + NewsAPI."""
    logger.info("📰 Fetching news...")
    count = await fetch_and_store_news()
    logger.info(f"✅ Stored {count} new articles")


async def job_daily_digest():
    """
    Daily 9:00 AM IST — Send top recommendations via Telegram.

    Reads deliverable recommendations from today's scan and sends them
    using the Telegram bot.

    Skipped on trading holidays (weekends + NSE holidays).

    If Kite is configured but not authenticated, sends a login link
    and queues the digest to auto-deliver after login.
    """
    from datetime import date as _date
    from src.data.nse_holidays import is_trading_day

    today = _date.today()

    if not is_trading_day(today):
        logger.info(f"📅 Today ({today}) is a trading holiday — digest skipped")
        return

    logger.info("📊 Preparing daily digest...")

    if not settings.has_telegram:
        logger.info("Telegram not configured — digest skipped")
        return

    try:
        from telegram import Bot
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from src.bot.handler import deliver_daily_digest, _pending_after_login

        bot = Bot(token=settings.telegram_bot_token)

        # If Kite is configured, check auth before sending digest
        if settings.has_kite:
            from src.trading.kite_client import is_authenticated
            if not is_authenticated():
                # Queue digest for after login and send login link
                _pending_after_login.add("digest")

                from src.trading.kite_client import get_login_url
                login_url = get_login_url()

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 Login to Zerodha", url=login_url)]
                ])

                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=(
                        "📊 *Daily Digest Ready*\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Data ingestion complete ✅\n"
                        "Login to Zerodha to get recommendations\n"
                        "with live market prices.\n\n"
                        "Digest will be sent automatically after login.\n"
                        "━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                logger.info("Kite not authenticated — digest queued pending login")
                return

        await deliver_daily_digest(bot)
        logger.info("📊 Daily digest delivered via Telegram")
    except Exception as e:
        logger.error(f"Daily digest delivery failed: {e}")


async def job_portfolio_sync():
    """Daily 9:00 AM IST — Sync holdings from Zerodha."""
    logger.info("📂 Syncing portfolio...")
    try:
        from src.trading.kite_client import sync_holdings_from_kite
        count = await sync_holdings_from_kite()
        logger.info(f"✅ Synced {count} holdings")
    except Exception as e:
        logger.warning(f"Portfolio sync failed: {e}")


def register_jobs(scheduler):
    """
    Register all scheduled jobs with APScheduler.

    Args:
        scheduler: APScheduler AsyncIOScheduler instance
    """
    # Full scan — daily at 6:00 AM IST
    scheduler.add_job(
        job_full_scan,
        "cron",
        hour=settings.full_scan_hour,
        minute=0,
        timezone="Asia/Kolkata",
        id="full_scan",
        name="Full Stock Scan",
        replace_existing=True,
    )

    # Daily digest — daily at 9:00 AM IST
    scheduler.add_job(
        job_daily_digest,
        "cron",
        hour=settings.digest_hour,
        minute=0,
        timezone="Asia/Kolkata",
        id="daily_digest",
        name="Daily Recommendation Digest",
        replace_existing=True,
    )

    # Bulk deal check — daily at 4:00 PM IST
    scheduler.add_job(
        job_bulk_deal_check,
        "cron",
        hour=settings.bulk_deal_hour,
        minute=0,
        timezone="Asia/Kolkata",
        id="bulk_deal_check",
        name="Bulk Deal Check",
        replace_existing=True,
    )

    # News scan — every 30 minutes (run immediately on startup)
    from datetime import datetime as dt
    scheduler.add_job(
        job_news_scan,
        "interval",
        minutes=30,
        next_run_time=dt.now(),
        id="news_scan",
        name="News Scan",
        replace_existing=True,
    )

    # Portfolio sync — daily at 9:00 AM IST (same as digest)
    scheduler.add_job(
        job_portfolio_sync,
        "cron",
        hour=9,
        minute=0,
        timezone="Asia/Kolkata",
        id="portfolio_sync",
        name="Portfolio Sync",
        replace_existing=True,
    )

    # Data cleanup — daily at midnight IST
    scheduler.add_job(
        job_data_cleanup,
        "cron",
        hour=0,
        minute=0,
        timezone="Asia/Kolkata",
        id="data_cleanup",
        name="Data Retention Cleanup",
        replace_existing=True,
    )

    logger.info(
        f"Registered {len(scheduler.get_jobs())} scheduled jobs "
        f"(scan={settings.full_scan_hour}:00, "
        f"digest={settings.digest_hour}:00, "
        f"deals={settings.bulk_deal_hour}:00, "
        f"cleanup=00:00)"
    )
