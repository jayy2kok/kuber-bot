"""
Data Retention Cleanup — purge old records to keep the DB lean.

Retention policy:
  Stock Prices:     2 years
  Bulk Deals:       10 days
  Fundamentals:     Latest per stock + 30 days history
  Recommendations:  Until target date + 30 days
                    (target date = scan_date + holding period)
  News Articles:    30 days

Designed for low-resource environments (Raspberry Pi).
"""

import logging
from datetime import date, timedelta

from sqlalchemy import delete, select, func, and_

from src.db.engine import get_session
from src.db.models import (
    StockPrice,
    BulkDeal,
    Fundamental,
    Recommendation,
    NewsArticle,
    HoldingPeriod,
)

logger = logging.getLogger(__name__)

# ─── Retention Periods ────────────────────────────────────────────────────────

STOCK_PRICE_RETENTION_DAYS = 2 * 365       # 2 years
BULK_DEAL_RETENTION_DAYS = 10              # 10 days
FUNDAMENTAL_RETENTION_DAYS = 30            # 30 days (keep latest per stock always)
RECOMMENDATION_BUFFER_DAYS = 30            # 30 days past target date
NEWS_RETENTION_DAYS = 30                   # 30 days

# Holding period → approximate duration in days
HOLDING_PERIOD_DAYS = {
    HoldingPeriod.MEDIUM_TERM: 180,        # 6 months (upper bound of 3-6 months)
    HoldingPeriod.SUPER_LONG_TERM: 365,    # 1 year
}


async def cleanup_stock_prices() -> int:
    """Delete stock prices older than 2 years."""
    cutoff = date.today() - timedelta(days=STOCK_PRICE_RETENTION_DAYS)

    async with get_session() as session:
        result = await session.execute(
            delete(StockPrice).where(StockPrice.date < cutoff)
        )
        deleted = result.rowcount

    if deleted:
        logger.info(f"Cleaned up {deleted} stock price rows older than {cutoff}")
    return deleted


async def cleanup_bulk_deals() -> int:
    """Delete bulk deals older than 10 days."""
    cutoff = date.today() - timedelta(days=BULK_DEAL_RETENTION_DAYS)

    async with get_session() as session:
        result = await session.execute(
            delete(BulkDeal).where(BulkDeal.deal_date < cutoff)
        )
        deleted = result.rowcount

    if deleted:
        logger.info(f"Cleaned up {deleted} bulk deal rows older than {cutoff}")
    return deleted


async def cleanup_fundamentals() -> int:
    """
    Delete fundamentals older than 30 days, but always keep the latest
    record per stock regardless of age.
    """
    cutoff = date.today() - timedelta(days=FUNDAMENTAL_RETENTION_DAYS)

    async with get_session() as session:
        # Find the latest as_of_date per stock
        latest_per_stock = (
            select(
                Fundamental.stock_id,
                func.max(Fundamental.as_of_date).label("max_date"),
            )
            .group_by(Fundamental.stock_id)
            .subquery()
        )

        # Delete old fundamentals that are NOT the latest for their stock
        result = await session.execute(
            delete(Fundamental).where(
                and_(
                    Fundamental.as_of_date < cutoff,
                    ~Fundamental.id.in_(
                        select(Fundamental.id).join(
                            latest_per_stock,
                            and_(
                                Fundamental.stock_id == latest_per_stock.c.stock_id,
                                Fundamental.as_of_date == latest_per_stock.c.max_date,
                            ),
                        )
                    ),
                )
            )
        )
        deleted = result.rowcount

    if deleted:
        logger.info(f"Cleaned up {deleted} fundamental rows older than {cutoff}")
    return deleted


async def cleanup_recommendations() -> int:
    """
    Delete recommendations past their target date + 30 days buffer.

    Target date is computed as:
      scan_date + holding_period_days + buffer_days

    Where holding_period_days depends on the holding_period enum:
      MEDIUM_TERM     → 180 days (upper bound of 3-6 months)
      SUPER_LONG_TERM → 365 days (1 year)
    """
    today = date.today()
    total_deleted = 0

    async with get_session() as session:
        for period, duration_days in HOLDING_PERIOD_DAYS.items():
            cutoff = today - timedelta(days=duration_days + RECOMMENDATION_BUFFER_DAYS)

            result = await session.execute(
                delete(Recommendation).where(
                    and_(
                        Recommendation.holding_period == period,
                        Recommendation.scan_date < cutoff,
                    )
                )
            )
            total_deleted += result.rowcount

    if total_deleted:
        logger.info(f"Cleaned up {total_deleted} expired recommendation rows")
    return total_deleted


async def cleanup_news() -> int:
    """Delete news articles older than 30 days."""
    cutoff = date.today() - timedelta(days=NEWS_RETENTION_DAYS)

    async with get_session() as session:
        result = await session.execute(
            delete(NewsArticle).where(NewsArticle.published_at < cutoff)
        )
        deleted = result.rowcount

    if deleted:
        logger.info(f"Cleaned up {deleted} news article rows older than {cutoff}")
    return deleted


async def job_data_cleanup() -> dict:
    """
    Run all cleanup tasks. Called by scheduler at midnight IST.

    Returns a summary dict with counts of deleted rows per table.
    """
    logger.info("🧹 Starting data cleanup...")

    results = {
        "stock_prices": await cleanup_stock_prices(),
        "bulk_deals": await cleanup_bulk_deals(),
        "fundamentals": await cleanup_fundamentals(),
        "recommendations": await cleanup_recommendations(),
        "news_articles": await cleanup_news(),
    }

    total = sum(results.values())
    logger.info(f"🧹 Cleanup complete — {total} total rows deleted: {results}")
    return results
