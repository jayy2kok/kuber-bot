"""
Watchlist Engine — recommendation performance tracking.

Responsibilities:
  - sync_accepted_status(): cross-reference delivered BUY recs with live holdings
  - refresh_watchlist_cmp(): batch-refresh CMP and evaluate target/SL milestones
  - get_watchlist_data(): compile accepted + not-accepted recs with P&L
  - get_watchlist_summary(): aggregate stats for the summary cards
"""

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_

from src.db.engine import get_session
from src.db.models import Recommendation, Holding, Stock, SignalType

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


# ─── Accepted Status Sync ─────────────────────────────────────────────────────


async def sync_accepted_status() -> int:
    """
    Cross-reference all delivered BUY recommendations with current Zerodha
    holdings and mark matching ones as accepted.

    - A recommendation is "accepted" when the stock is (or was) found in the
      live (non-paper) portfolio after the recommendation was delivered.
    - Once accepted, the flag is never rolled back even if the stock is sold
      later (it's a historical record of the decision).
    - The matching holding's quantity and average_price are stored on the rec
      so the UI can compute real P&L for accepted entries.

    Returns the number of recs newly marked as accepted.
    """
    async with get_session() as session:
        # All live holdings keyed by symbol
        holdings_result = await session.execute(
            select(Holding).where(Holding.is_paper == False)
        )
        holdings_by_symbol: dict[str, Holding] = {
            h.symbol: h for h in holdings_result.scalars().all()
        }

        # All delivered BUY recs not yet accepted
        recs_result = await session.execute(
            select(Recommendation).where(
                and_(
                    Recommendation.is_delivered == True,
                    Recommendation.signal.in_([
                        SignalType.BUY.value, SignalType.STRONG_BUY.value
                    ]),
                    Recommendation.is_accepted == False,
                )
            )
        )
        recs = recs_result.scalars().all()

        newly_accepted = 0
        for rec in recs:
            # Load stock symbol
            stock_result = await session.execute(
                select(Stock).where(Stock.id == rec.stock_id)
            )
            stock = stock_result.scalar_one_or_none()
            if not stock:
                continue

            if stock.symbol in holdings_by_symbol:
                rec.is_accepted = True
                # Initialise watchlist_status if not set yet
                if not rec.watchlist_status:
                    rec.watchlist_status = "active"
                newly_accepted += 1

    if newly_accepted:
        logger.info(f"Watchlist: marked {newly_accepted} recommendation(s) as accepted")
    return newly_accepted


# ─── CMP Refresh & Status Evaluation ─────────────────────────────────────────


async def refresh_watchlist_cmp() -> int:
    """
    Batch-refresh CMP for all delivered recommendations and evaluate
    target/stop-loss milestones.

    Price sources (in order):
      1. Zerodha Kite LTP (if authenticated)
      2. Yahoo Finance regularMarketPrice (fallback)

    Status transitions (irreversible once hit):
      - CMP >= target_2           → "target_2_hit"
      - CMP >= target_1 (& < t2) → "target_1_hit"
      - CMP <= stop_loss          → "sl_hit"
      - otherwise                 → "active"

    Returns the number of recommendations updated.
    """
    async with get_session() as session:
        # Fetch all delivered recs that haven't hit a final milestone yet
        result = await session.execute(
            select(Recommendation).where(
                and_(
                    Recommendation.is_delivered == True,
                    Recommendation.watchlist_status.in_(["active", None]),
                )
            )
        )
        recs = result.scalars().all()

    if not recs:
        return 0

    # Collect all unique symbols
    stock_ids = list({r.stock_id for r in recs})
    async with get_session() as session:
        stocks_result = await session.execute(
            select(Stock).where(Stock.id.in_(stock_ids))
        )
        id_to_symbol = {s.id: s.symbol for s in stocks_result.scalars().all()}

    symbols = list(id_to_symbol.values())

    # ── Source 1: Zerodha Kite LTP (primary) ──
    prices: dict[str, float] = {}
    try:
        from src.trading.kite_client import fetch_ltp_batch, is_authenticated
        if is_authenticated():
            prices = await fetch_ltp_batch(symbols)
            logger.info(f"Kite LTP: got prices for {len(prices)}/{len(symbols)} symbols")
        else:
            logger.info("Kite not authenticated — will use Yahoo Finance fallback")
    except Exception as e:
        logger.warning(f"Kite LTP batch failed: {e}")

    # ── Source 2: Yahoo Finance fallback for missing symbols ──
    missing_symbols = [s for s in symbols if s not in prices]
    if missing_symbols:
        yahoo_prices = await _fetch_yahoo_ltp_batch(missing_symbols)
        prices.update(yahoo_prices)
        logger.info(
            f"Yahoo fallback: got prices for {len(yahoo_prices)}/{len(missing_symbols)} "
            f"missing symbols"
        )

    if not prices:
        logger.warning("Watchlist CMP refresh: no prices from Kite or Yahoo")
        return 0

    now = datetime.utcnow()
    updated = 0

    async with get_session() as session:
        for rec in recs:
            symbol = id_to_symbol.get(rec.stock_id)
            if not symbol or symbol not in prices:
                continue

            cmp = prices[symbol]

            # Skip NaN/Inf prices
            if math.isnan(cmp) or math.isinf(cmp):
                continue

            # Re-fetch rec in this session for update
            db_rec = await session.get(Recommendation, rec.id)
            if not db_rec:
                continue

            db_rec.cmp = cmp
            db_rec.cmp_last_refresh = now

            # Evaluate milestones (only progress forward, never regress)
            if db_rec.target_2 and cmp >= db_rec.target_2:
                if db_rec.watchlist_status != "target_2_hit":
                    db_rec.watchlist_status = "target_2_hit"
                    db_rec.status_hit_at = now
            elif cmp >= db_rec.target_1:
                if db_rec.watchlist_status not in ("target_1_hit", "target_2_hit"):
                    db_rec.watchlist_status = "target_1_hit"
                    db_rec.status_hit_at = now
            elif cmp <= db_rec.stop_loss:
                if db_rec.watchlist_status != "sl_hit":
                    db_rec.watchlist_status = "sl_hit"
                    db_rec.status_hit_at = now
            else:
                if not db_rec.watchlist_status:
                    db_rec.watchlist_status = "active"

            updated += 1

    logger.info(f"Watchlist CMP refresh: updated {updated} recommendations")
    return updated


async def _fetch_yahoo_ltp_batch(symbols: list[str]) -> dict[str, float]:
    """
    Fetch last traded prices from Yahoo Finance as fallback.

    Uses yfinance's Ticker.info['regularMarketPrice'] for each symbol.
    """
    import asyncio

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — Yahoo fallback unavailable")
        return {}

    prices: dict[str, float] = {}

    async def _fetch_one(sym: str):
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            info = await asyncio.to_thread(lambda: ticker.info)
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if price is not None and not math.isnan(float(price)):
                prices[sym] = float(price)
        except Exception as e:
            logger.debug(f"Yahoo LTP failed for {sym}: {e}")

    # Fetch in parallel with a concurrency limit to avoid rate limiting
    semaphore = asyncio.Semaphore(5)

    async def _limited_fetch(sym: str):
        async with semaphore:
            await _fetch_one(sym)
            await asyncio.sleep(0.1)  # Small delay to avoid rate limiting

    await asyncio.gather(*[_limited_fetch(s) for s in symbols])
    return prices


# ─── Data Retrieval ───────────────────────────────────────────────────────────


def _pnl_for_accepted(rec: Recommendation, holding: Optional[Holding]) -> dict:
    """
    Compute P&L for an accepted recommendation using actual portfolio values.
    """
    if holding:
        qty = holding.quantity
        avg_price = holding.average_price
        invested = qty * avg_price
    else:
        # Holding may have been sold since acceptance — use entry_price with
        # a notional 1-share as denominator so the % is still meaningful
        qty = 1
        avg_price = rec.entry_price
        invested = avg_price

    cmp = rec.cmp or avg_price
    current_value = qty * cmp
    pnl_abs = current_value - invested
    pnl_pct = (pnl_abs / invested * 100) if invested > 0 else 0

    return {
        "qty": qty,
        "avg_price": round(avg_price, 2),
        "invested": round(invested, 2),
        "current_value": round(current_value, 2),
        "pnl_abs": round(pnl_abs, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


def _pnl_for_notional(rec: Recommendation, notional: float = 1000.0) -> dict:
    """
    Compute hypothetical P&L using a notional ₹1,000 investment.
    """
    entry = rec.entry_price or rec.cmp or 1
    cmp = rec.cmp or entry
    qty_notional = notional / entry  # fractional units
    current_value = qty_notional * cmp
    pnl_abs = current_value - notional
    pnl_pct = (pnl_abs / notional * 100) if notional > 0 else 0

    return {
        "qty": None,
        "avg_price": None,
        "invested": round(notional, 2),
        "current_value": round(current_value, 2),
        "pnl_abs": round(pnl_abs, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


async def get_watchlist_data() -> dict:
    """
    Return full watchlist payload — accepted and not-accepted recommendations
    with P&L computed appropriately for each group.

    Returns:
        {
            "accepted": [...],
            "not_accepted": [...],
            "last_refresh": "ISO8601 string or null",
        }
    """
    async with get_session() as session:
        # All delivered recommendations, newest first
        recs_result = await session.execute(
            select(Recommendation)
            .where(Recommendation.is_delivered == True)
            .order_by(Recommendation.scan_date.desc(), Recommendation.id.desc())
        )
        recs = recs_result.scalars().all()

        # Stock lookup
        stock_ids = list({r.stock_id for r in recs})
        stocks_result = await session.execute(
            select(Stock).where(Stock.id.in_(stock_ids))
        )
        stocks_by_id = {s.id: s for s in stocks_result.scalars().all()}

        # Live holdings for accepted recs
        holdings_result = await session.execute(
            select(Holding).where(Holding.is_paper == False)
        )
        holdings_by_symbol = {h.symbol: h for h in holdings_result.scalars().all()}

    accepted = []
    not_accepted = []
    last_refresh: Optional[datetime] = None


    def _clean_dict(d: dict) -> dict:
        for k, v in d.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                d[k] = 0.0
        return d

    # Track seen stock_ids to deduplicate — only show the latest rec per stock
    seen_accepted = set()
    seen_not_accepted = set()

    for rec in recs:
        stock = stocks_by_id.get(rec.stock_id)
        if not stock:
            continue

        if rec.cmp_last_refresh and (
            last_refresh is None or rec.cmp_last_refresh > last_refresh
        ):
            last_refresh = rec.cmp_last_refresh

        base = {
            "id": rec.id,
            "date": rec.scan_date.isoformat(),
            "symbol": stock.symbol,
            "name": stock.name,
            "signal": rec.signal,
            "entry_price": rec.entry_price,
            "cmp": rec.cmp,
            "target_1": rec.target_1,
            "target_2": rec.target_2,
            "stop_loss": rec.stop_loss,
            "risk_reward": rec.risk_reward,
            "watchlist_status": rec.watchlist_status or "active",
            "status_hit_at": rec.status_hit_at.isoformat() if rec.status_hit_at else None,
            "composite_score": rec.composite_score,
            "last_refresh": rec.cmp_last_refresh.isoformat() if rec.cmp_last_refresh else None,
        }

        if rec.is_accepted:
            if rec.stock_id in seen_accepted:
                continue  # Skip older duplicate for this stock
            seen_accepted.add(rec.stock_id)
            holding = holdings_by_symbol.get(stock.symbol)
            pnl = _pnl_for_accepted(rec, holding)
            accepted.append(_clean_dict({**base, **pnl, "is_accepted": True}))
        else:
            if rec.stock_id in seen_not_accepted:
                continue  # Skip older duplicate for this stock
            seen_not_accepted.add(rec.stock_id)
            pnl = _pnl_for_notional(rec)
            not_accepted.append(_clean_dict({**base, **pnl, "is_accepted": False}))

    return {
        "accepted": accepted,
        "not_accepted": not_accepted,
        "last_refresh": last_refresh.isoformat() if last_refresh else None,
    }


async def get_watchlist_summary() -> dict:
    """
    Compute aggregate summary metrics for the watchlist summary cards.
    """
    data = await get_watchlist_data()

    def _agg(recs: list) -> dict:
        if not recs:
            return {
                "count": 0,
                "total_invested": 0,
                "total_current": 0,
                "total_pnl_abs": 0,
                "avg_pnl_pct": 0,
                "winners": 0,
                "losers": 0,
                "target_hits": 0,
                "sl_hits": 0,
            }
        total_invested = sum(r["invested"] for r in recs)
        total_current = sum(r["current_value"] for r in recs)
        total_pnl = total_current - total_invested
        avg_pnl = sum(r["pnl_pct"] for r in recs) / len(recs)
        winners = sum(1 for r in recs if r["pnl_pct"] > 0)
        losers = sum(1 for r in recs if r["pnl_pct"] < 0)
        target_hits = sum(
            1 for r in recs
            if r["watchlist_status"] in ("target_1_hit", "target_2_hit")
        )
        sl_hits = sum(1 for r in recs if r["watchlist_status"] == "sl_hit")
        return {
            "count": len(recs),
            "total_invested": round(total_invested, 2),
            "total_current": round(total_current, 2),
            "total_pnl_abs": round(total_pnl, 2),
            "avg_pnl_pct": round(avg_pnl, 2),
            "winners": winners,
            "losers": losers,
            "target_hits": target_hits,
            "sl_hits": sl_hits,
        }

    acc = _agg(data["accepted"])
    not_acc = _agg(data["not_accepted"])

    # Overall across all delivered recs
    missed_gain = sum(
        r["pnl_abs"] for r in data["not_accepted"]
        if r["watchlist_status"] in ("target_1_hit", "target_2_hit")
    )

    return {
        "total_recommendations": acc["count"] + not_acc["count"],
        "accepted": acc,
        "not_accepted": not_acc,
        "missed_gains": round(missed_gain, 2),
        "last_refresh": data["last_refresh"],
    }
