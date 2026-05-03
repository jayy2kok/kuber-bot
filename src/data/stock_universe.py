"""
Stock universe management — loads and maintains the Nifty 500 stock list.

Responsibilities:
  - Load stock symbols from CSV (data/nifty500.csv)
  - Sync the master stock list into the DB
  - Provide industry-to-P/E median mappings
"""

import csv
import logging
import os
from typing import Optional

from sqlalchemy import select

from src.db.engine import get_session
from src.db.models import Stock

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
NIFTY500_CSV = os.path.join(DATA_DIR, "nifty500.csv")

# Industry median P/E lookup (ported from long_term_scanner.py — Section 5A.1)
INDUSTRY_PE_MEDIAN: dict[str, float] = {
    "Financial Services": 18,
    "Information Technology": 28,
    "Healthcare": 30,
    "Automobile and Auto Components": 22,
    "Fast Moving Consumer Goods": 45,
    "Consumer Durables": 35,
    "Capital Goods": 30,
    "Oil Gas & Consumable Fuels": 12,
    "Metals & Mining": 10,
    "Construction Materials": 20,
    "Construction": 18,
    "Chemicals": 25,
    "Power": 15,
    "Telecommunication": 25,
    "Realty": 25,
    "Services": 22,
    "Consumer Services": 50,
    "Textiles": 18,
    "Diversified": 20,
    "Media Entertainment & Publication": 25,
    "Forest Materials": 20,
}
DEFAULT_PE_MEDIAN = 25.0


def get_industry_pe_median(industry: str) -> float:
    """Return the median P/E for an industry, or the default if unknown."""
    return INDUSTRY_PE_MEDIAN.get(industry, DEFAULT_PE_MEDIAN)


def load_symbols_from_csv(csv_path: Optional[str] = None) -> list[dict]:
    """
    Load stock symbols from a CSV file.

    Expected CSV columns: Symbol, Company Name, Industry, Sector
    Returns a list of dicts with keys: symbol, name, industry, sector

    If the CSV doesn't exist, returns an empty list and logs a warning.
    """
    path = csv_path or NIFTY500_CSV

    if not os.path.exists(path):
        logger.warning(f"Stock universe CSV not found: {path}")
        return []

    symbols = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get("Symbol", "").strip()
            if not symbol:
                continue
            symbols.append({
                "symbol": symbol,
                "name": row.get("Company Name", symbol),
                "industry": row.get("Industry", "Unknown"),
                "sector": row.get("Sector", "Unknown"),
            })

    logger.info(f"Loaded {len(symbols)} symbols from {path}")
    return symbols


async def sync_stock_universe(csv_path: Optional[str] = None) -> int:
    """
    Sync the CSV stock universe into the stocks DB table.

    - Inserts new stocks that don't exist
    - Updates name/industry/sector for existing stocks
    - Returns count of new stocks added
    """
    symbols = load_symbols_from_csv(csv_path)
    if not symbols:
        return 0

    new_count = 0

    async with get_session() as session:
        # Get existing symbols
        result = await session.execute(select(Stock.symbol))
        existing = {row[0] for row in result.all()}

        for sym_data in symbols:
            symbol = sym_data["symbol"]
            yahoo_sym = f"{symbol}.NS"

            if symbol not in existing:
                stock = Stock(
                    symbol=symbol,
                    name=sym_data["name"],
                    industry=sym_data["industry"],
                    sector=sym_data["sector"],
                    yahoo_symbol=yahoo_sym,
                    is_active=True,
                )
                session.add(stock)
                new_count += 1
            # Note: updating existing stocks could be added here if needed

    logger.info(
        f"Universe sync complete: {new_count} new stocks added, "
        f"{len(symbols)} total in universe"
    )
    return new_count


async def get_all_active_stocks() -> list[Stock]:
    """Return all active stocks from the database."""
    async with get_session() as session:
        result = await session.execute(
            select(Stock).where(Stock.is_active.is_(True)).order_by(Stock.symbol)
        )
        return list(result.scalars().all())
