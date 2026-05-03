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
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select, and_

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import (
    Trade, Holding, GTTOrder, OrderStatus, TradeSide, GTTStatus,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Lazy-loaded Kite instance
_kite = None


# ─── P5.1 Authentication ─────────────────────────────────────────────────────


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
    kite = _get_kite_instance()
    try:
        session_data = await asyncio.to_thread(
            kite.generate_session,
            request_token,
            api_secret=settings.kite_api_secret,
        )
        access_token = session_data["access_token"]
        kite.set_access_token(access_token)
        logger.info(f"Kite session established for {session_data.get('user_name', 'user')}")
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


def is_authenticated() -> bool:
    """Check if Kite has a valid access token set."""
    try:
        kite = _get_kite_instance()
        return kite.access_token is not None and kite.access_token != ""
    except Exception:
        return False


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
            "http://localhost:5000/kite/login"
        )
        return -1  # -1 = not authenticated (distinct from 0 = no holdings)

    kite = _get_kite_instance()

    try:
        holdings = await asyncio.to_thread(kite.holdings)
    except Exception as e:
        logger.error(f"Failed to fetch Kite holdings: {e}")
        return 0

    synced = 0
    async with get_session() as session:
        for h in holdings:
            symbol = h.get("tradingsymbol", "")
            if not symbol:
                continue

            # Check if holding exists
            result = await session.execute(
                select(Holding).where(
                    and_(
                        Holding.symbol == symbol,
                        Holding.is_paper == False,
                    )
                )
            )
            existing = result.scalar_one_or_none()

            qty = h.get("quantity", 0)
            avg_price = h.get("average_price", 0)
            last_price = h.get("last_price", avg_price)
            pnl = h.get("pnl", 0)
            pnl_pct = ((last_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

            if existing:
                existing.quantity = qty
                existing.average_price = avg_price
                existing.last_price = last_price
                existing.pnl = pnl
                existing.pnl_pct = round(pnl_pct, 2)
                existing.synced_at = datetime.utcnow()
            else:
                new_holding = Holding(
                    symbol=symbol,
                    quantity=qty,
                    average_price=avg_price,
                    last_price=last_price,
                    pnl=pnl,
                    pnl_pct=round(pnl_pct, 2),
                    is_paper=False,
                    synced_at=datetime.utcnow(),
                )
                session.add(new_holding)

            synced += 1

    logger.info(f"Synced {synced} holdings from Zerodha")
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
