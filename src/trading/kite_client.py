"""
Zerodha Kite Connect client — OAuth, orders, GTT, and portfolio sync.

Reference: Plan.md Phase 5

Provides:
  - P5.1: OAuth2 login flow + access token management
  - P5.2: Order placement (market/limit) with retry logic
  - P5.3: GTT order management (target + stop-loss)
  - P5.4: Portfolio sync (holdings from Zerodha)
"""

import asyncio
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import (
    Trade, Holding, GTTOrder, OrderStatus, TradeSide, GTTStatus,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Lazy-loaded Kite instance
_kite = None

# Track when the access token was generated (IST date).
# Kite tokens expire daily around 6:00 AM IST, so we treat any
# token from a previous calendar day as expired.
_auth_date: Optional[date] = None


# ─── P5.1 Authentication ─────────────────────────────────────────────────────

# IST offset for date comparison
_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    """Get today's date in IST."""
    return datetime.now(_IST).date()


def get_login_url() -> str:
    """
    Generate the Kite Connect OAuth2 login URL.

    The user must open this URL in a browser, log in to Zerodha,
    and provide the resulting request_token back to FBot.
    """
    base_url = "https://kite.zerodha.com/connect/login"
    params = {"v": 3, "api_key": settings.kite_api_key}
    return f"{base_url}?{urlencode(params)}"


async def generate_session(request_token: str) -> dict:
    """
    Exchange a request_token for an access_token.

    This must be called once per trading day after browser login.
    Returns the session dict with access_token, user info, etc.
    """
    global _auth_date

    kite = _get_kite_instance()
    try:
        session_data = await asyncio.to_thread(
            kite.generate_session,
            request_token,
            api_secret=settings.kite_api_secret,
        )
        access_token = session_data["access_token"]
        kite.set_access_token(access_token)
        _auth_date = _today_ist()
        logger.info(
            f"Kite session established for {session_data.get('user_name', 'user')} "
            f"(auth_date={_auth_date})"
        )
        return session_data
    except Exception as e:
        logger.error(f"Kite session generation failed: {e}")
        raise


def _get_kite_instance():
    """Get or create the KiteConnect instance."""
    global _kite
    if _kite is None:
        if not settings.has_kite:
            raise RuntimeError("Kite Connect not configured — set KITE_API_KEY and KITE_API_SECRET")
        try:
            from kiteconnect import KiteConnect
            _kite = KiteConnect(api_key=settings.kite_api_key)
            if settings.kite_access_token:
                _kite.set_access_token(settings.kite_access_token)
        except ImportError:
            raise RuntimeError("kiteconnect package not installed")
    return _kite


def _invalidate_session() -> None:
    """Clear the stale access token so a fresh login is required."""
    global _auth_date
    try:
        kite = _get_kite_instance()
        kite.set_access_token(None)
    except Exception:
        pass
    _auth_date = None
    logger.info("Kite session invalidated — fresh login required")


def is_authenticated() -> bool:
    """
    Check if Kite has a valid (non-expired) access token.

    Kite access tokens expire daily around 6:00 AM IST.
    We consider a token valid only if:
      1. An access token is set on the KiteConnect instance, AND
      2. The token was generated today (IST calendar day).

    If the token is from a previous day, it is automatically cleared
    so that /login will present the login screen again.
    """
    try:
        kite = _get_kite_instance()
        has_token = kite.access_token is not None and kite.access_token != ""
    except Exception:
        return False

    if not has_token:
        return False

    # If we don't know when the token was generated (e.g. loaded from
    # env var on first container start), validate with a lightweight API call.
    if _auth_date is None:
        return _validate_token_live()

    # Token from a previous day → expired
    if _auth_date < _today_ist():
        logger.info(
            f"Kite token from {_auth_date} is stale (today={_today_ist()}) — invalidating"
        )
        _invalidate_session()
        return False

    return True


def _validate_token_live() -> bool:
    """
    Validate the current token with a lightweight Kite API call.

    Used as a one-time check when _auth_date is unknown (e.g. token
    was loaded from the KITE_ACCESS_TOKEN env var at startup).
    """
    global _auth_date

    try:
        kite = _get_kite_instance()
        # kite.profile() is the lightest authenticated call
        profile = kite.profile()
        if profile:
            _auth_date = _today_ist()
            logger.info(f"Kite token validated via API (user={profile.get('user_name', '?')})")
            return True
    except Exception as e:
        logger.info(f"Kite token validation failed: {e} — clearing stale token")
        _invalidate_session()

    return False


# ─── Live Price Fetching ─────────────────────────────────────────────────────


async def fetch_ltp(symbol: str) -> Optional[float]:
    """
    Fetch the Last Traded Price (LTP) for a single stock from Kite.

    Returns the LTP as a float, or None if Kite is not authenticated
    or the request fails.

    Uses the format: kite.ltp("NSE:SYMBOL") -> {"NSE:SYMBOL": {"last_price": ...}}
    """
    if not is_authenticated():
        return None

    kite = _get_kite_instance()
    instrument = f"NSE:{symbol}"

    try:
        data = await asyncio.to_thread(kite.ltp, instrument)
        if instrument in data:
            return float(data[instrument]["last_price"])
    except Exception as e:
        logger.debug(f"LTP fetch failed for {symbol}: {e}")

    return None


async def fetch_ltp_batch(symbols: list[str]) -> dict[str, float]:
    """
    Fetch LTP for multiple stocks from Kite in batches.

    Kite API allows up to ~200 instruments per call.
    We batch in groups of 50 to be safe.

    Args:
        symbols: List of NSE symbols (e.g. ["RELIANCE", "INFY", ...])

    Returns:
        Dict mapping symbol -> last_price. Missing symbols are omitted.
    """
    if not is_authenticated():
        return {}

    kite = _get_kite_instance()
    result = {}
    batch_size = 50

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        instruments = [f"NSE:{s}" for s in batch]

        try:
            data = await asyncio.to_thread(kite.ltp, *instruments)
            for sym in batch:
                key = f"NSE:{sym}"
                if key in data and "last_price" in data[key]:
                    result[sym] = float(data[key]["last_price"])
        except Exception as e:
            logger.warning(f"LTP batch fetch failed for batch starting at {i}: {e}")

        # Small delay between batches to avoid rate limiting
        if i + batch_size < len(symbols):
            await asyncio.sleep(0.2)

    logger.info(f"Fetched live LTP for {len(result)}/{len(symbols)} stocks from Kite")
    return result


# ─── P5.2 Order Placement ────────────────────────────────────────────────────


async def place_order(
    symbol: str,
    side: TradeSide,
    quantity: int,
    price: Optional[float] = None,
    order_type: str = "MARKET",
    product: str = "CNC",
    max_retries: int = 3,
) -> Optional[str]:
    """
    Place an order on Zerodha Kite.

    Args:
        symbol: NSE trading symbol (e.g. 'RELIANCE')
        side: BUY or SELL
        quantity: Number of shares
        price: Limit price (required for LIMIT orders)
        order_type: MARKET or LIMIT
        product: CNC (delivery) or MIS (intraday)
        max_retries: Retry count for failed orders

    Returns:
        Order ID string on success, None on failure.
    """
    kite = _get_kite_instance()

    transaction_type = "BUY" if side == TradeSide.BUY else "SELL"

    for attempt in range(1, max_retries + 1):
        try:
            order_params = {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "transaction_type": transaction_type,
                "quantity": quantity,
                "order_type": order_type,
                "product": product,
            }
            if order_type == "LIMIT" and price:
                order_params["price"] = price

            order_id = await asyncio.to_thread(kite.place_order, variety="regular", **order_params)

            logger.info(f"Order placed: {transaction_type} {symbol} x{quantity} — ID: {order_id}")
            return str(order_id)

        except Exception as e:
            logger.warning(f"Order attempt {attempt}/{max_retries} failed for {symbol}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"All {max_retries} order attempts failed for {symbol}")
                return None


async def place_order_from_trade(trade: Trade) -> bool:
    """
    Execute a Trade record via Kite Connect.

    Updates the Trade record with order ID and status.
    Returns True if order was placed successfully.
    """
    if settings.is_paper_mode:
        # Paper trade — just mark as executed
        async with get_session() as session:
            db_trade = await session.get(Trade, trade.id)
            if db_trade:
                db_trade.status = OrderStatus.EXECUTED
                db_trade.executed_at = datetime.utcnow()
                db_trade.order_id = f"PAPER-{trade.id}"
        logger.info(f"Paper trade executed: {trade.side} {trade.symbol} x{trade.quantity}")
        return True

    order_id = await place_order(
        symbol=trade.symbol,
        side=TradeSide(trade.side),
        quantity=trade.quantity,
        price=trade.price,
    )

    async with get_session() as session:
        db_trade = await session.get(Trade, trade.id)
        if db_trade:
            if order_id:
                db_trade.order_id = order_id
                db_trade.status = OrderStatus.PLACED
            else:
                db_trade.status = OrderStatus.FAILED

    return order_id is not None


# ─── P5.3 GTT Order Management ───────────────────────────────────────────────


async def set_gtt_order(
    symbol: str,
    trigger_type: str,
    trigger_price: float,
    quantity: int,
    limit_price: Optional[float] = None,
) -> Optional[str]:
    """
    Place a Good-Till-Triggered (GTT) order.

    Args:
        symbol: Trading symbol
        trigger_type: "target" or "stoploss"
        trigger_price: The trigger price
        quantity: Number of shares
        limit_price: Limit price for the order (defaults to trigger_price)

    Returns:
        GTT trigger ID on success, None on failure.
    """
    if settings.is_paper_mode:
        logger.info(f"Paper GTT: {trigger_type} for {symbol} @ ₹{trigger_price}")
        return f"PAPER-GTT-{symbol}-{trigger_type}"

    kite = _get_kite_instance()
    lp = limit_price or trigger_price

    try:
        # Single trigger GTT
        transaction_type = "SELL" if trigger_type == "target" else "SELL"

        trigger_id = await asyncio.to_thread(
            kite.place_gtt,
            trigger_type="single",
            tradingsymbol=symbol,
            exchange="NSE",
            trigger_values=[trigger_price],
            last_price=trigger_price,
            orders=[{
                "transaction_type": transaction_type,
                "quantity": quantity,
                "price": lp,
                "order_type": "LIMIT",
                "product": "CNC",
            }],
        )

        logger.info(f"GTT set: {trigger_type} {symbol} @ ₹{trigger_price} — ID: {trigger_id}")
        return str(trigger_id)

    except Exception as e:
        logger.error(f"GTT placement failed for {symbol}: {e}")
        return None


async def set_gtt_for_trade(trade: Trade, target: float, stop_loss: float) -> None:
    """
    Set both target and stop-loss GTT orders for a trade.

    Creates GTTOrder records in DB for tracking.
    """
    # Target GTT
    target_id = await set_gtt_order(
        symbol=trade.symbol,
        trigger_type="target",
        trigger_price=target,
        quantity=trade.quantity,
    )

    async with get_session() as session:
        gtt_target = GTTOrder(
            trade_id=trade.id,
            symbol=trade.symbol,
            trigger_type="target",
            trigger_price=target,
            order_quantity=trade.quantity,
            kite_gtt_id=target_id,
            status=GTTStatus.ACTIVE if target_id else GTTStatus.CANCELLED,
            is_paper=settings.is_paper_mode,
        )
        session.add(gtt_target)

    # Stop-loss GTT
    sl_id = await set_gtt_order(
        symbol=trade.symbol,
        trigger_type="stoploss",
        trigger_price=stop_loss,
        quantity=trade.quantity,
    )

    async with get_session() as session:
        gtt_sl = GTTOrder(
            trade_id=trade.id,
            symbol=trade.symbol,
            trigger_type="stoploss",
            trigger_price=stop_loss,
            order_quantity=trade.quantity,
            kite_gtt_id=sl_id,
            status=GTTStatus.ACTIVE if sl_id else GTTStatus.CANCELLED,
            is_paper=settings.is_paper_mode,
        )
        session.add(gtt_sl)

    logger.info(f"GTT orders set for {trade.symbol}: target=₹{target}, SL=₹{stop_loss}")


# ─── P5.4 Portfolio Sync ─────────────────────────────────────────────────────


async def sync_holdings_from_kite() -> int:
    """
    Sync holdings from Zerodha Kite to the DB.

    Strategy: delete all existing non-paper holdings, then bulk-insert
    the current Zerodha snapshot.  This guarantees the DB always mirrors
    Zerodha exactly — sold stocks are removed automatically.

    Works in BOTH paper and live modes — holdings from your real Zerodha
    account are always needed for accurate sell signal generation.

    Returns the number of holdings synced.
    """
    if not settings.has_kite:
        logger.info("Kite Connect not configured — cannot sync holdings")
        return 0

    if not is_authenticated():
        logger.warning(
            "Kite not authenticated — login first via /kite/login or "
            f"{settings.base_url}/kite/login"
        )
        return -1  # -1 = not authenticated (distinct from 0 = no holdings)

    kite = _get_kite_instance()

    try:
        holdings = await asyncio.to_thread(kite.holdings)
    except Exception as e:
        logger.error(f"Failed to fetch Kite holdings: {e}")
        return 0

    now = datetime.utcnow()

    async with get_session() as session:
        # ── Step 1: wipe all existing non-paper holdings ──────────────────
        from sqlalchemy import delete as sa_delete
        await session.execute(
            sa_delete(Holding).where(Holding.is_paper == False)
        )

        # ── Step 2: insert the fresh Zerodha snapshot ─────────────────────
        synced = 0
        for h in holdings:
            symbol = h.get("tradingsymbol", "")
            if not symbol:
                continue

            avg_price = h.get("average_price", 0)
            last_price = h.get("last_price", avg_price)
            pnl = h.get("pnl", 0)
            pnl_pct = ((last_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

            session.add(Holding(
                symbol=symbol,
                quantity=h.get("quantity", 0),
                average_price=avg_price,
                last_price=last_price,
                pnl=pnl,
                pnl_pct=round(pnl_pct, 2),
                is_paper=False,
                source="zerodha",
                synced_at=now,
            ))
            synced += 1

    logger.info(f"Synced {synced} holdings from Zerodha (replaced entire portfolio)")
    return synced



async def get_portfolio_value() -> dict:
    """
    Get current portfolio value and P&L summary.

    Returns dict with total_value, total_pnl, holdings_count.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Holding).where(Holding.quantity > 0)
        )
        holdings = list(result.scalars().all())

    total_value = sum((h.last_price or h.average_price) * h.quantity for h in holdings)
    total_invested = sum(h.average_price * h.quantity for h in holdings)
    total_pnl = total_value - total_invested

    return {
        "total_value": round(total_value, 2),
        "total_invested": round(total_invested, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_pct": round((total_pnl / total_invested * 100) if total_invested > 0 else 0, 2),
        "holdings_count": len(holdings),
    }
