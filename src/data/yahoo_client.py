"""
Yahoo Finance data client — wrapper around yfinance.

Provides:
  - Historical OHLCV data download (with caching)
  - Fundamental info fetching (P/E, ROE, EPS, etc.)
  - Batch downloading for the full stock universe
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from sqlalchemy import select, func

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import Stock, StockPrice, Fundamental

logger = logging.getLogger(__name__)

# ─── Symbol Helpers ───────────────────────────────────────────────────────────


def to_yahoo_symbol(nse_symbol: str) -> str:
    """Convert an NSE symbol to Yahoo Finance format (append .NS)."""
    return f"{nse_symbol}.NS"


# ─── Historical OHLCV ────────────────────────────────────────────────────────


async def fetch_ohlcv(
    symbol: str,
    period: str = "2y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Download historical OHLCV data from Yahoo Finance.

    Args:
        symbol: NSE symbol (e.g. 'RELIANCE')
        period: yfinance period string ('1y', '2y', etc.)
        interval: candle interval ('1d', '1wk', etc.)

    Returns:
        DataFrame with columns [Open, High, Low, Close, Volume, Adj Close]
        or None if download fails.
    """
    yahoo_sym = to_yahoo_symbol(symbol)
    try:
        df = await asyncio.to_thread(
            yf.download,
            yahoo_sym,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            logger.warning(f"No OHLCV data for {symbol}")
            return None

        # Flatten multi-level columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.reset_index()
        return df
    except Exception as e:
        logger.error(f"Failed to fetch OHLCV for {symbol}: {e}")
        return None


async def save_ohlcv_to_db(stock_id: int, df: pd.DataFrame) -> int:
    """
    Persist OHLCV DataFrame to the stock_prices table.

    Skips dates that already exist (upsert by unique constraint).
    Returns count of new rows inserted.
    """
    inserted = 0
    async with get_session() as session:
        # Get existing dates for this stock
        result = await session.execute(
            select(StockPrice.date).where(StockPrice.stock_id == stock_id)
        )
        existing_dates = {row[0] for row in result.all()}

        for _, row in df.iterrows():
            row_date = pd.Timestamp(row["Date"]).date()
            if row_date in existing_dates:
                continue

            # Skip rows with NaN/Inf in any critical OHLCV field
            ohlcv_vals = [row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]]
            if any(pd.isna(v) for v in ohlcv_vals):
                logger.debug(f"Skipping NaN OHLCV row for stock_id={stock_id} on {row_date}")
                continue

            price = StockPrice(
                stock_id=stock_id,
                date=row_date,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]),
                adj_close=float(row.get("Adj Close", row["Close"])),
            )
            session.add(price)
            inserted += 1

    logger.info(f"Inserted {inserted} new price rows for stock_id={stock_id}")
    return inserted


# ─── Fundamental Info ─────────────────────────────────────────────────────────


async def fetch_info(symbol: str) -> Optional[dict]:
    """
    Fetch the yfinance .info dict for a stock.

    Returns the raw dict or None if fetch fails.
    """
    yahoo_sym = to_yahoo_symbol(symbol)
    try:
        ticker = yf.Ticker(yahoo_sym)
        info = await asyncio.to_thread(lambda: ticker.info)
        if not info or info.get("regularMarketPrice") is None:
            logger.warning(f"No info data for {symbol}")
            return None
        return info
    except Exception as e:
        logger.error(f"Failed to fetch info for {symbol}: {e}")
        return None


async def save_fundamentals_to_db(stock_id: int, info: dict, industry_pe_median: float) -> None:
    """
    Extract fundamental metrics from yfinance info dict and persist.

    Mirrors the metrics used by the GARP scanner (Section 5A.1).
    """
    import math

    def _safe_float(val) -> Optional[float]:
        """Sanitize float values — asyncpg rejects Infinity/NaN."""
        if val is None:
            return None
        try:
            f = float(val)
            if math.isinf(f) or math.isnan(f):
                return None
            return f
        except (ValueError, TypeError):
            return None

    roe = info.get("returnOnEquity")
    if roe is not None:
        roe_pct = roe * 100
    else:
        # Fallback: compute ROE from EPS and Book Value
        # ROE ≈ (Trailing EPS / Book Value per share) × 100
        eps = info.get("trailingEps")
        bv = info.get("bookValue")
        if eps and bv and bv > 0:
            roe_pct = (eps / bv) * 100
            logger.debug(f"Computed ROE from EPS/BV: {roe_pct:.1f}%")
        else:
            roe_pct = None

    de_ratio = info.get("debtToEquity")
    de_ratio = de_ratio / 100 if de_ratio is not None else None

    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")
    if trailing_eps and forward_eps and trailing_eps > 0:
        eps_growth = ((forward_eps - trailing_eps) / trailing_eps) * 100
    else:
        eg = info.get("earningsGrowth")
        eps_growth = eg * 100 if eg else None

    rev_growth = info.get("revenueGrowth")
    rev_growth_pct = rev_growth * 100 if rev_growth else None

    promoter_pct = info.get("heldPercentInsiders")
    promoter_pct = promoter_pct * 100 if promoter_pct else None

    fii_pct = info.get("heldPercentInstitutions")
    fii_pct = fii_pct * 100 if fii_pct else None

    market_cap = info.get("marketCap", 0)
    market_cap_cr = market_cap / 1e7 if market_cap else None

    today = date.today()

    async with get_session() as session:
        # Check if we already have fundamentals for this stock today
        existing = await session.execute(
            select(Fundamental).where(
                Fundamental.stock_id == stock_id,
                Fundamental.as_of_date == today,
            )
        )
        fundamental = existing.scalar_one_or_none()

        if fundamental is None:
            fundamental = Fundamental(stock_id=stock_id, as_of_date=today)
            session.add(fundamental)

        # Update all fields (works for both insert and update)
        fundamental.pe_ratio = _safe_float(info.get("trailingPE"))
        fundamental.forward_pe = _safe_float(info.get("forwardPE"))
        fundamental.trailing_eps = _safe_float(trailing_eps)
        fundamental.forward_eps = _safe_float(forward_eps)
        fundamental.eps_growth_pct = _safe_float(eps_growth)
        fundamental.roe_pct = _safe_float(roe_pct)
        fundamental.debt_to_equity = _safe_float(de_ratio)
        fundamental.revenue_growth_pct = _safe_float(rev_growth_pct)
        fundamental.market_cap_cr = _safe_float(market_cap_cr)
        fundamental.book_value = _safe_float(info.get("bookValue"))
        fundamental.pb_ratio = _safe_float(info.get("priceToBook"))
        fundamental.dividend_yield_pct = _safe_float(
            info.get("dividendYield", 0) * 100 if info.get("dividendYield") else None
        )
        fundamental.promoter_holding_pct = _safe_float(promoter_pct)
        fundamental.fii_holding_pct = _safe_float(fii_pct)
        fundamental.industry_pe_median = _safe_float(industry_pe_median)

    logger.info(f"Saved fundamentals for stock_id={stock_id}")


# ─── Batch Operations ────────────────────────────────────────────────────────


async def fetch_and_store_stock_data(
    symbol: str,
    stock_id: int,
    industry_pe_median: float,
    period: str = "2y",
    delay: float = 0.1,
) -> bool:
    """
    Full data pipeline for one stock: OHLCV + Fundamentals.

    Returns True if both succeeded.
    """
    success = True

    # 1. OHLCV
    try:
        df = await fetch_ohlcv(symbol, period=period)
        if df is not None:
            await save_ohlcv_to_db(stock_id, df)
        else:
            success = False
    except Exception as e:
        logger.error(f"OHLCV failed for {symbol}: {e}")
        success = False

    # 2. Fundamentals
    try:
        info = await fetch_info(symbol)
        if info is not None:
            await save_fundamentals_to_db(stock_id, info, industry_pe_median)
        else:
            success = False
    except Exception as e:
        logger.error(f"Fundamentals failed for {symbol}: {e}")
        success = False

    # Throttle to avoid rate limiting
    await asyncio.sleep(delay)
    return success
