"""
Telegram Bot — command handlers, inline keyboards, and approval flow.

Reference: Plan.md Phase 4

Provides:
  - /start, /status, /scan, /portfolio, /holdings, /refresh commands
  - Inline keyboard for recommendation approval/rejection
  - Daily digest delivery
  - Error alerting via Telegram
"""

import logging
from datetime import date, datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from sqlalchemy import select, and_, func

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import (
    Recommendation, Stock, Holding, Trade, SignalType, OrderStatus, TradeSide,
    BulkDeal, DealCategory,
)
from src.bot.formatters import (
    format_recommendation_card,
    format_daily_digest_header,
    format_portfolio_summary,
    format_error_alert,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Pending actions that wait for Kite login before executing
_pending_after_login: set[str] = set()


# ─── Inline Keyboard Builder ─────────────────────────────────────────────────


def build_approval_keyboard(recommendation_id: int) -> InlineKeyboardMarkup:
    """
    Build the approval inline keyboard (Plan.md P4.3).

    [✅ Approve Trade]  [❌ Reject]
    [✏️ Modify Qty]     [⏸️ Defer]
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{recommendation_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{recommendation_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Modify Qty", callback_data=f"modify:{recommendation_id}"),
            InlineKeyboardButton("⏸️ Defer", callback_data=f"defer:{recommendation_id}"),
        ],
    ])


# ─── Command Handlers ────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "📈 *FBot — Stock Market Scanner & Trading Bot*\n\n"
        "*Analysis*\n"
        "/analyze \<stock\> — Analyze a specific stock\n"
        "/scan — Fetch data + run full analysis\n"
        "/digest — Get today's recommendations\n\n"
        "*Trading*\n"
        "/login — Login to Zerodha (daily)\n"
        "/sync — Sync holdings from Zerodha\n"
        "/fetch — Download market data (first-time setup)\n\n"
        "*Portfolio*\n"
        "/portfolio — Portfolio summary\n"
        "/holdings — Current holdings\n\n"
        "*Admin*\n"
        "/status — System status\n"
        "/refresh — Refresh Nifty 500 list from NSE\n"
        "/help — Show this help\n",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command — show system status."""
    async with get_session() as session:
        stock_count = await session.scalar(
            select(func.count()).select_from(Stock)
        )
        rec_today = await session.scalar(
            select(func.count()).select_from(Recommendation).where(
                Recommendation.scan_date == date.today()
            )
        )
        holding_count = await session.scalar(
            select(func.count()).select_from(Holding).where(Holding.quantity > 0)
        )

    await update.message.reply_text(
        f"📊 *FBot Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: {settings.trading_mode.value.upper()}\n"
        f"Stocks in universe: {stock_count}\n"
        f"Today's recommendations: {rec_today}\n"
        f"Active holdings: {holding_count}\n"
        f"Zerodha: {'✅ Connected' if settings.has_kite else '❌ Not configured'}\n"
        f"Ollama: {'✅ Available' if settings.has_ollama else '❌ Not available'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan command — fetch data + run full scan pipeline."""
    await update.message.reply_text(
        "🔄 Starting daily scan...\n"
        "Auto-detecting data gap + fetching missing days.\n"
        "⏳ Please wait..."
    )

    try:
        from src.scheduler.jobs import job_full_scan

        recommendations = await job_full_scan()
        await update.message.reply_text(
            f"✅ Scan complete! Generated {len(recommendations)} deliverable recommendations."
        )

        # Send recommendations
        for rec in recommendations:
            await _send_recommendation(context.bot, rec)

    except Exception as e:
        logger.error(f"Manual scan failed: {e}")
        await update.message.reply_text(f"❌ Scan failed: {str(e)[:200]}")


async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /fetch command — fetch all market data (OHLCV + fundamentals + bulk deals)."""
    await update.message.reply_text(
        "📊 Starting full market data fetch...\n"
        "1️⃣ OHLCV + fundamentals for all stocks\n"
        "2️⃣ Bulk/block deals from NSE\n"
        "3️⃣ News articles from RSS feeds\n"
        "⏳ This may take 15-30 minutes for 500 stocks."
    )

    try:
        from src.data.yahoo_client import fetch_and_store_stock_data
        from src.data.stock_universe import get_all_active_stocks, get_industry_pe_median
        from src.data.nse_client import backfill_deals

        # ── Step 1: OHLCV + Fundamentals ──
        stocks = await get_all_active_stocks()
        total = len(stocks)
        success, fail = 0, 0

        for i, stock in enumerate(stocks):
            industry_pe = get_industry_pe_median(stock.industry)
            ok = await fetch_and_store_stock_data(
                symbol=stock.symbol,
                stock_id=stock.id,
                industry_pe_median=industry_pe,
                period="2y",
                delay=0.1,
            )
            if ok:
                success += 1
            else:
                fail += 1

            if (i + 1) % 50 == 0:
                await update.message.reply_text(
                    f"📊 Progress: {i + 1}/{total} stocks fetched "
                    f"({success} ✅, {fail} ❌)"
                )

        # ── Step 2: Bulk/Block Deals ──
        await update.message.reply_text("🏦 Fetching bulk/block deals from NSE...")
        inst_count = await backfill_deals(days=30)

        # Get deal totals from DB
        async with get_session() as session:
            result = await session.execute(
                select(func.count()).select_from(
                    select(BulkDeal.id).subquery()
                )
            )
            deal_total = result.scalar() or 0

            result = await session.execute(
                select(func.count()).select_from(
                    select(BulkDeal.id).where(
                        BulkDeal.category.in_([DealCategory.FII, DealCategory.DII])
                    ).subquery()
                )
            )
            inst_total = result.scalar() or 0

        # ── Step 3: News Articles ──
        await update.message.reply_text("📰 Fetching news articles...")
        from src.data.news_client import fetch_and_store_news
        news_count = await fetch_and_store_news()

        await update.message.reply_text(
            f"✅ *Full data fetch complete!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *Stocks:* {total} ({success} ✅, {fail} ❌)\n"
            f"🏦 *Deals:* {deal_total} total ({inst_total} institutional)\n"
            f"📰 *News:* {news_count} new articles\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Now run /scan to generate recommendations.",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        await update.message.reply_text(f"❌ Fetch failed: {str(e)[:200]}")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /portfolio command — show portfolio summary."""
    async with get_session() as session:
        result = await session.execute(
            select(Holding).where(Holding.quantity > 0)
        )
        holdings = list(result.scalars().all())

    if not holdings:
        await update.message.reply_text("📭 No holdings found.")
        return

    total_value = sum((h.last_price or h.average_price) * h.quantity for h in holdings)
    total_pnl = sum(h.pnl or 0 for h in holdings)

    msg = format_portfolio_summary(holdings, total_value, total_pnl)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_holdings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /holdings command — list current holdings."""
    async with get_session() as session:
        result = await session.execute(
            select(Holding).where(Holding.quantity > 0).order_by(Holding.symbol)
        )
        holdings = list(result.scalars().all())

    if not holdings:
        await update.message.reply_text("📭 No holdings found.")
        return

    lines = ["📋 *Current Holdings*\n━━━━━━━━━━━━━━━━━━━━━━"]
    for h in holdings:
        pnl_pct = f"{h.pnl_pct:+.1f}%" if h.pnl_pct else "N/A"
        lines.append(
            f"• {h.symbol}: {h.quantity} shares @ ₹{h.average_price:,.2f} ({pnl_pct})"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /digest command — show today's recommendations.

    Requires Kite login for live prices. If not authenticated,
    sends a login link and queues digest to run automatically
    after successful login.
    """
    if not settings.has_kite:
        # No Kite configured — just send digest with stale prices
        await deliver_daily_digest(context.bot)
        return

    from src.trading.kite_client import is_authenticated

    if is_authenticated():
        # Already logged in — deliver immediately with live prices
        await deliver_daily_digest(context.bot)
        return

    # Not authenticated — send login link and queue digest
    _pending_after_login.add("digest")

    from src.trading.kite_client import get_login_url
    login_url = get_login_url()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Login to Zerodha", url=login_url)]
    ])

    await update.message.reply_text(
        "🔐 *Login Required for Live Prices*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Digest needs live market prices from Zerodha.\n\n"
        "1️⃣ Tap the button below to login\n"
        "2️⃣ After login, your digest will be sent automatically\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /refresh command — download latest Nifty 500 list from NSE and re-sync."""
    await update.message.reply_text("🔄 Downloading latest Nifty 500 list from NSE India...")

    try:
        import asyncio
        from src.data.download_nifty500 import fetch_nifty500_from_nse, save_to_csv
        from src.data.stock_universe import sync_stock_universe

        # Download runs sync HTTP — offload to thread
        stocks = await asyncio.to_thread(fetch_nifty500_from_nse)

        if not stocks:
            await update.message.reply_text(
                "❌ Could not fetch stock list from NSE.\n"
                "NSE may be blocking requests — try again during market hours."
            )
            return

        # Save updated CSV
        await asyncio.to_thread(save_to_csv, stocks)

        # Re-sync universe to DB
        new_count = await sync_stock_universe()

        # Get total count
        async with get_session() as session:
            total = await session.scalar(
                select(func.count()).select_from(Stock).where(Stock.is_active == True)
            )

        await update.message.reply_text(
            f"✅ *Stock universe refreshed!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Downloaded: {len(stocks)} stocks from NSE\n"
            f"New stocks added: {new_count or 0}\n"
            f"Total in universe: {total}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Stock list refresh failed: {e}")
        await update.message.reply_text(f"❌ Refresh failed: {str(e)[:200]}")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sync command — sync holdings from Zerodha Kite account."""
    if not settings.has_kite:
        await update.message.reply_text(
            "❌ Zerodha Kite Connect not configured.\n"
            "Set KITE_API_KEY and KITE_API_SECRET in .env"
        )
        return

    await update.message.reply_text("🔄 Syncing holdings from Zerodha...")

    try:
        from src.trading.kite_client import sync_holdings_from_kite, is_authenticated

        if not is_authenticated():
            # Show login URL
            from src.trading.kite_client import get_login_url
            login_url = get_login_url()
            await update.message.reply_text(
                "⚠️ *Kite Connect not authenticated*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "You need to login to Zerodha first (once per day):\n\n"
                f"1️⃣ Open in browser:\n`{settings.base_url}/kite/login`\n\n"
                "2️⃣ Login with your Zerodha credentials\n\n"
                "3️⃣ After login, come back and run /sync again\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
            )
            return

        count = await sync_holdings_from_kite()

        if count > 0:
            # Show summary
            from src.trading.kite_client import get_portfolio_value
            portfolio = await get_portfolio_value()

            await update.message.reply_text(
                f"✅ *Holdings synced from Zerodha!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Stocks: {portfolio['holdings_count']}\n"
                f"Total Value: ₹{portfolio['total_value']:,.2f}\n"
                f"Total P&L: ₹{portfolio['total_pnl']:+,.2f} "
                f"({portfolio['pnl_pct']:+.1f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
            )
        elif count == 0:
            await update.message.reply_text("📭 No holdings found in your Zerodha account.")
        else:
            # count == -1, auth error (shouldn't reach here but just in case)
            await update.message.reply_text("⚠️ Authentication issue. Try /sync again.")

    except Exception as e:
        logger.error(f"Portfolio sync failed: {e}")
        await update.message.reply_text(f"❌ Sync failed: {str(e)[:200]}")


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /login command — send Kite Connect login link."""
    if not settings.has_kite:
        await update.message.reply_text(
            "❌ Zerodha Kite Connect not configured.\n"
            "Set KITE_API_KEY and KITE_API_SECRET in .env"
        )
        return

    from src.trading.kite_client import is_authenticated

    if is_authenticated():
        await update.message.reply_text(
            "✅ Already logged in to Zerodha for today!\n"
            "Use /sync to refresh holdings or /scan to run analysis."
        )
        return

    from src.trading.kite_client import get_login_url
    login_url = get_login_url()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Login to Zerodha", url=login_url)]
    ])

    await update.message.reply_text(
        "🔐 *Zerodha Daily Login*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap the button below to login.\n"
        "After login, FBot will automatically:\n\n"
        "✅ Sync your holdings\n"
        "✅ Notify you here with portfolio summary\n"
        "✅ Enable sell signals for held stocks\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ─── Callback Query Handler (Inline Keyboard) ────────────────────────────────────────


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for approval flow."""
    query = update.callback_query
    await query.answer()

    data = query.data
    action, id_str = data.split(":", 1)

    # Route analyze: callbacks (stock_id) separately from rec_id callbacks
    if action == "analyze":
        await _handle_analyze_callback(query, int(id_str))
        return

    rec_id = int(id_str)

    if action == "approve":
        await _handle_approve(query, rec_id)
    elif action == "reject":
        await _handle_reject(query, rec_id)
    elif action == "modify":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✏️ Reply with the new quantity for recommendation #{rec_id}.\n"
            f"Format: /modify {rec_id} <quantity>"
        )
    elif action == "defer":
        await _handle_defer(query, rec_id)


async def _handle_approve(query, rec_id: int) -> None:
    """Approve a recommendation and trigger trade placement."""
    async with get_session() as session:
        rec = await session.get(Recommendation, rec_id)
        if rec is None:
            await query.edit_message_text("❌ Recommendation not found.")
            return

        rec.is_approved = True
        rec.approved_at = datetime.utcnow()

        # Create a trade record
        stock = await session.get(Stock, rec.stock_id)
        symbol = stock.symbol if stock else "UNKNOWN"

        side = TradeSide.BUY if rec.signal in (SignalType.STRONG_BUY, SignalType.BUY) else TradeSide.SELL

        trade = Trade(
            recommendation_id=rec.id,
            symbol=symbol,
            side=side,
            quantity=rec.suggested_qty or 1,
            price=rec.entry_price,
            status=OrderStatus.PENDING,
            is_paper=settings.is_paper_mode,
        )
        session.add(trade)

    await query.edit_message_reply_markup(reply_markup=None)

    if settings.is_paper_mode:
        await query.message.reply_text(
            f"✅ Approved! 📝 Paper trade created for {symbol}.\n"
            f"(Live trading not enabled — set TRADING_MODE=live)"
        )
    else:
        await query.message.reply_text(
            f"✅ Approved! Placing {side.value.upper()} order for {symbol}..."
        )
        # Trigger actual Kite order (Phase 5)
        try:
            from src.trading.kite_client import place_order_from_trade
            await place_order_from_trade(trade)
        except Exception as e:
            await query.message.reply_text(f"⚠️ Order placement failed: {e}")


async def _handle_reject(query, rec_id: int) -> None:
    """Reject a recommendation."""
    async with get_session() as session:
        rec = await session.get(Recommendation, rec_id)
        if rec:
            rec.is_approved = False
            rec.approved_at = datetime.utcnow()

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("❌ Recommendation rejected.")


async def _handle_defer(query, rec_id: int) -> None:
    """Defer a recommendation for later review."""
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("⏸️ Deferred. You can review later with /digest.")


# ─── Single Stock Analysis ────────────────────────────────────────────────────


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /analyze <stock> — analyze a specific stock on demand.

    Supports symbol (e.g. RELIANCE) or name (e.g. "Reliance Industries").
    Uses fuzzy search to find the closest match.
    """
    if not context.args:
        await update.message.reply_text(
            "📊 *Usage:* `/analyze <stock name or symbol>`\n\n"
            "*Examples:*\n"
            "  `/analyze RELIANCE`\n"
            "  `/analyze HDFC Bank`\n"
            "  `/analyze asian paint`\n",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"🔍 Searching for \"{query}\"...")

    try:
        from src.engine.recommender import find_stock_fuzzy
        matches = await find_stock_fuzzy(query)
    except Exception as e:
        await update.message.reply_text(f"❌ Search failed: {str(e)[:200]}")
        return

    if not matches:
        await update.message.reply_text(
            f"❌ No stocks found matching \"{query}\"\n"
            "Try using the NSE symbol (e.g. RELIANCE) or a partial name."
        )
        return

    if len(matches) == 1:
        # Single match — analyze directly
        await _run_single_analysis(update, context, matches[0])
    else:
        # Multiple matches — show inline keyboard for selection
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{s.symbol} — {s.name[:30]}",
                callback_data=f"analyze:{s.id}",
            )]
            for s in matches[:5]
        ])
        await update.message.reply_text(
            f"📋 Multiple matches found for \"{query}\".\nSelect one:",
            reply_markup=keyboard,
        )


async def _handle_analyze_callback(query, stock_id: int) -> None:
    """Handle stock selection from /analyze inline keyboard."""
    await query.edit_message_reply_markup(reply_markup=None)

    async with get_session() as session:
        stock = await session.get(Stock, stock_id)

    if not stock:
        await query.message.reply_text("❌ Stock not found.")
        return

    await query.message.reply_text(
        f"📊 Analyzing *{stock.symbol}* ({stock.name})...\n"
        "⏳ Running fundamental + technical + news + AI analysis...",
        parse_mode="Markdown",
    )

    try:
        from src.engine.recommender import analyze_single_stock
        rec = await analyze_single_stock(stock)
    except Exception as e:
        await query.message.reply_text(f"❌ Analysis failed: {str(e)[:200]}")
        return

    if rec is None:
        await query.message.reply_text(
            f"⚠️ Cannot analyze {stock.symbol} — insufficient data.\n"
            "Ensure price data is available (run /fetch first)."
        )
        return

    # Fetch institutional deals
    from datetime import timedelta
    institutional_deals = []
    try:
        async with get_session() as session:
            cutoff = date.today() - timedelta(days=10)
            result = await session.execute(
                select(BulkDeal).where(
                    and_(
                        BulkDeal.symbol == stock.symbol,
                        BulkDeal.deal_date >= cutoff,
                        BulkDeal.category.in_([DealCategory.FII, DealCategory.DII]),
                    )
                ).order_by(BulkDeal.deal_date.desc())
            )
            institutional_deals = list(result.scalars().all())
    except Exception as e:
        logger.warning(f"Could not fetch deals for {stock.symbol}: {e}")

    card = format_recommendation_card(
        rec, stock.symbol, stock.name,
        institutional_deals=institutional_deals,
    )
    await query.message.reply_text(card)


async def _run_single_analysis(
    update: Update, context: ContextTypes.DEFAULT_TYPE, stock: Stock,
) -> None:
    """Run analysis on a single stock and send the card."""
    await update.message.reply_text(
        f"📊 Analyzing *{stock.symbol}* ({stock.name})...\n"
        "⏳ Running fundamental + technical + news + AI analysis...",
        parse_mode="Markdown",
    )

    try:
        from src.engine.recommender import analyze_single_stock
        rec = await analyze_single_stock(stock)
    except Exception as e:
        await update.message.reply_text(f"❌ Analysis failed: {str(e)[:200]}")
        return

    if rec is None:
        await update.message.reply_text(
            f"⚠️ Cannot analyze {stock.symbol} — insufficient data.\n"
            "Ensure price data is available (run /fetch first)."
        )
        return

    # Fetch institutional deals
    from datetime import timedelta
    institutional_deals = []
    try:
        async with get_session() as session:
            cutoff = date.today() - timedelta(days=10)
            result = await session.execute(
                select(BulkDeal).where(
                    and_(
                        BulkDeal.symbol == stock.symbol,
                        BulkDeal.deal_date >= cutoff,
                        BulkDeal.category.in_([DealCategory.FII, DealCategory.DII]),
                    )
                ).order_by(BulkDeal.deal_date.desc())
            )
            institutional_deals = list(result.scalars().all())
    except Exception as e:
        logger.warning(f"Could not fetch deals for {stock.symbol}: {e}")

    card = format_recommendation_card(
        rec, stock.symbol, stock.name,
        institutional_deals=institutional_deals,
    )
    await update.message.reply_text(card)


# ─── Delivery Functions ──────────────────────────────────────────────────────


async def _send_recommendation(bot, rec: Recommendation) -> None:
    """Send a single recommendation with approval keyboard."""
    async with get_session() as session:
        stock = await session.get(Stock, rec.stock_id)

    if stock is None:
        return

    # Fetch recent institutional deals for this stock (last 10 days)
    from datetime import timedelta
    institutional_deals = []
    try:
        async with get_session() as session:
            cutoff = date.today() - timedelta(days=10)
            result = await session.execute(
                select(BulkDeal).where(
                    and_(
                        BulkDeal.symbol == stock.symbol,
                        BulkDeal.deal_date >= cutoff,
                        BulkDeal.category.in_([DealCategory.FII, DealCategory.DII]),
                    )
                ).order_by(BulkDeal.deal_date.desc())
            )
            institutional_deals = list(result.scalars().all())
    except Exception as e:
        logger.warning(f"Could not fetch institutional deals for {stock.symbol}: {e}")

    card = format_recommendation_card(
        rec, stock.symbol, stock.name,
        institutional_deals=institutional_deals,
    )
    keyboard = build_approval_keyboard(rec.id)

    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=card,
            reply_markup=keyboard,
        )
        # Mark as delivered
        async with get_session() as session:
            db_rec = await session.get(Recommendation, rec.id)
            if db_rec:
                db_rec.is_delivered = True
                db_rec.delivered_at = datetime.utcnow()

    except Exception as e:
        logger.error(f"Failed to send recommendation {rec.id}: {e}")


async def deliver_daily_digest(bot) -> None:
    """
    Send the daily digest — top 10 buy + all sell recommendations.

    Called by the scheduler job (P6.1) and /digest command.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Recommendation).where(
                and_(
                    Recommendation.scan_date == date.today(),
                    Recommendation.is_delivered == True,
                )
            ).order_by(Recommendation.composite_score.desc())
        )
        recommendations = list(result.scalars().all())

        total_scanned = await session.scalar(
            select(func.count()).select_from(Stock).where(Stock.is_active == True)
        )

    if not recommendations:
        try:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text="📊 No recommendations for today. Market conditions may not be favorable.",
            )
        except Exception as e:
            logger.error(f"Failed to send empty digest: {e}")
        return

    buy_recs = [r for r in recommendations if r.signal in (SignalType.STRONG_BUY, SignalType.BUY)]
    sell_recs = [r for r in recommendations if r.signal in (SignalType.SELL, SignalType.STRONG_SELL)]

    # Send header
    header = format_daily_digest_header(date.today(), len(buy_recs), len(sell_recs), total_scanned)
    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id, text=header, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send digest header: {e}")

    # Send each recommendation with approval keyboard
    for rec in sell_recs + buy_recs:
        await _send_recommendation(bot, rec)


async def send_error_alert(bot, error_type: str, message: str) -> None:
    """Send an error alert to the admin via Telegram."""
    if not settings.has_telegram:
        return
    try:
        msg = format_error_alert(error_type, message)
        await bot.send_message(
            chat_id=settings.telegram_chat_id, text=msg, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send error alert: {e}")


async def run_pending_post_login_actions(bot) -> None:
    """
    Execute any commands that were queued pending Kite login.

    Called from the Kite OAuth callback (main.py) after successful authentication.
    """
    global _pending_after_login

    if not _pending_after_login:
        return

    pending = _pending_after_login.copy()
    _pending_after_login.clear()

    logger.info(f"Running {len(pending)} pending post-login actions: {pending}")

    for action in pending:
        try:
            if action == "digest":
                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text="✅ Zerodha login detected! Sending your digest with live prices...",
                )
                await deliver_daily_digest(bot)
            else:
                logger.warning(f"Unknown pending action: {action}")
        except Exception as e:
            logger.error(f"Failed to execute pending action '{action}': {e}")


# ─── Bot Application Builder ─────────────────────────────────────────────────


def create_bot_application() -> Optional[Application]:
    """
    Build and configure the Telegram bot application.

    Returns None if Telegram is not configured.
    """
    if not settings.has_telegram:
        logger.warning("Telegram not configured — bot disabled")
        return None

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("analyze", cmd_analyze))


    # Register callback handler for inline keyboards
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Telegram bot configured with all handlers")
    return app
