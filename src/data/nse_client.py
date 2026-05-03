"""
NSE India API client — fetches bulk/block deals and other NSE data.

Uses nsepython for session-managed requests to bypass NSE anti-bot measures.
Mirrors the data fetching logic from fii_scanner.py (Section 5A.2).
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import BulkDeal, DealCategory

logger = logging.getLogger(__name__)

# ─── FII/DII Classification (ported from fii_scanner.py) ─────────────────────

KNOWN_FII_NAMES = [
    # Global Investment Banks
    "GOLDMAN SACHS", "MORGAN STANLEY", "JP MORGAN", "JPMORGAN",
    "CITIGROUP", "CITIBANK", "CITI ", "HSBC", "UBS ", "UBS-",
    "CREDIT SUISSE", "BARCLAYS", "DEUTSCHE BANK", "BNP PARIBAS",
    "NOMURA", "MACQUARIE", "JEFFERIES", "CLSA", "SOCIETE GENERALE",
    # Major Asset Managers / Funds
    "BLACKROCK", "VANGUARD", "FIDELITY", "ABERDEEN", "TEMPLETON",
    "SCHRODER", "INVESCO", "CAPITAL GROUP", "T. ROWE PRICE",
    "WELLINGTON", "PIMCO", "AMUNDI", "ABRDN", "FRANKLIN",
    "DIMENSIONAL FUND", "STATE STREET", "NORTHERN TRUST",
    # Sovereign / Government Funds
    "GOVERNMENT OF SINGAPORE", "GIC PRIVATE", "GIC PTE",
    "TEMASEK", "ABU DHABI", "ADIA ", "NORWAY GOVERNMENT",
    "QATAR INVESTMENT", "KUWAIT INVESTMENT", "SAUDI ARABIA",
    "NORGES BANK",
    # PE / Hedge Funds
    "TIGER GLOBAL", "WARBURG PINCUS", "KKR ", "KKR-", "CARLYLE",
    "BAIN CAPITAL", "GENERAL ATLANTIC", "SEQUOIA", "SOFTBANK",
    "ACCEL PARTNERS", "PROSUS", "NASPERS", "ALIBABA",
    # Canada / Pension
    "CANADA PENSION", "CPPIB", "ONTARIO TEACHERS", "CAISSE DE", "CDPQ",
    # Specific FPIs
    "ELARA CAPITAL", "COPTHALL MAURITIUS", "NALANDA CAPITAL",
    "SMALLCAP WORLD FUND", "EUROPACIFIC GROWTH",
    "NEW WORLD FUND", "DODGE & COX",
]

FII_KEYWORDS = [
    "FPI", "FOREIGN", "OVERSEAS", "MAURITIUS", "SINGAPORE",
    "HONG KONG", "CAYMAN", "LUXEMBOURG", "IRELAND", "CYPRUS",
    "GLOBAL FUND", "INTERNATIONAL FUND", "OFFSHORE",
    "EMERGING MARKET", "ASIA FUND", "INDIA FUND",
]

DII_KEYWORDS = [
    "MUTUAL FUND", "LIFE INSURANCE", "GENERAL INSURANCE",
    "PENSION FUND", "PROVIDENT FUND", "ENDOWMENT",
    "SBI MUTUAL", "HDFC MUTUAL", "ICICI PRUDENTIAL",
    "KOTAK MUTUAL", "AXIS MUTUAL", "NIPPON INDIA MUTUAL",
    "BIRLA SUN", "TATA MUTUAL", "UTI MUTUAL", "DSP MUTUAL",
    "EDELWEISS MUTUAL", "MIRAE ASSET", "MOTILAL OSWAL MUTUAL",
    "LIC OF INDIA", "LIFE INSURANCE CORPORATION",
    "NATIONAL PENSION", "EMPLOYEES PROVIDENT",
]


def classify_client(client_name: str) -> DealCategory:
    """Classify a deal client as FII, DII, or OTHER based on name patterns."""
    name_upper = client_name.upper().strip()

    for pattern in KNOWN_FII_NAMES:
        if pattern in name_upper:
            return DealCategory.FII

    for kw in FII_KEYWORDS:
        if kw in name_upper:
            return DealCategory.FII

    for kw in DII_KEYWORDS:
        if kw in name_upper:
            return DealCategory.DII

    return DealCategory.OTHER


def _parse_deal_date(date_str: str) -> date:
    """Parse NSE deal date string (DD-Mon-YYYY or DD-MMM-YYYY) to date."""
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    # Fallback to today
    logger.warning(f"Could not parse deal date '{date_str}', using today")
    return date.today()


# ─── NSE Data Fetching ────────────────────────────────────────────────────────


async def fetch_bulk_block_deals() -> tuple[list[dict], list[dict]]:
    """
    Fetch today's bulk and block deals from NSE India API.

    Returns (bulk_deals, block_deals) — each a list of raw dicts.
    """
    try:
        from nsepython import nsefetch

        url = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
        data = await asyncio.to_thread(nsefetch, url)

        bulk_deals = data.get("BULK_DEALS_DATA", [])
        block_deals = data.get("BLOCK_DEALS_DATA", [])

        logger.info(f"Fetched {len(bulk_deals)} bulk + {len(block_deals)} block deals from NSE")
        return bulk_deals, block_deals

    except ImportError:
        logger.error("nsepython not installed — cannot fetch NSE deals")
        return [], []
    except Exception as e:
        logger.error(f"Failed to fetch deals from NSE: {e}")
        return [], []


async def fetch_historical_deals(
    from_date: date, to_date: date
) -> tuple[list[dict], list[dict]]:
    """
    Fetch historical bulk and block deals for a date range.

    Uses nsepython's historical deals endpoint.
    Dates must be within the last ~6 months (NSE API limitation).
    """
    try:
        from nsepython import nsefetch

        from_str = from_date.strftime("%d-%m-%Y")
        to_str = to_date.strftime("%d-%m-%Y")

        all_bulk = []
        all_block = []

        # Fetch bulk deals
        url = (
            f"https://www.nseindia.com/api/historical/bulk-deals"
            f"?from={from_str}&to={to_str}"
        )
        data = await asyncio.to_thread(nsefetch, url)
        if isinstance(data, dict):
            all_bulk = data.get("data", [])
        elif isinstance(data, list):
            all_bulk = data

        # Fetch block deals
        url = (
            f"https://www.nseindia.com/api/historical/block-deals"
            f"?from={from_str}&to={to_str}"
        )
        data = await asyncio.to_thread(nsefetch, url)
        if isinstance(data, dict):
            all_block = data.get("data", [])
        elif isinstance(data, list):
            all_block = data

        logger.info(
            f"Fetched {len(all_bulk)} historical bulk + "
            f"{len(all_block)} block deals ({from_str} to {to_str})"
        )
        return all_bulk, all_block

    except ImportError:
        logger.error("nsepython not installed — cannot fetch historical deals")
        return [], []
    except Exception as e:
        logger.error(f"Failed to fetch historical deals: {e}")
        return [], []


async def _store_deals(all_deals: list[dict], deal_type_default: str = "BULK") -> int:
    """
    Classify and persist deals to DB. Returns count of institutional deals.

    Deduplicates by (symbol, client_name, deal_date, quantity).
    """
    if not all_deals:
        logger.info("No deals to process")
        return 0

    institutional_count = 0
    new_count = 0
    skipped = 0

    async with get_session() as session:
        # Load existing deal keys for dedup
        from sqlalchemy import select
        result = await session.execute(
            select(
                BulkDeal.symbol,
                BulkDeal.client_name,
                BulkDeal.deal_date,
                BulkDeal.quantity,
            )
        )
        existing_keys = {
            (row[0], row[1], row[2], row[3])
            for row in result.all()
        }

        for deal in all_deals:
            client_name = deal.get("clientName", "") or ""
            symbol = deal.get("symbol", "") or ""
            category = classify_client(client_name)

            qty_str = str(deal.get("qty", "0") or "0").replace(",", "")
            price_str = str(deal.get("watp", "0") or "0").replace(",", "")

            try:
                qty = int(qty_str)
                price = float(price_str)
            except (ValueError, TypeError):
                continue

            # Parse date from deal data
            date_str = deal.get("date", "")
            deal_date = _parse_deal_date(date_str) if date_str else date.today()
            deal_type = deal.get("deal_type", deal_type_default)

            # Dedup check
            key = (symbol, client_name, deal_date, qty)
            if key in existing_keys:
                skipped += 1
                continue

            value_cr = (qty * price) / 1e7

            bulk_deal = BulkDeal(
                symbol=symbol,
                company_name=deal.get("name", "") or "",
                client_name=client_name,
                deal_type=deal_type,
                buy_sell=(deal.get("buySell", "") or "").upper(),
                quantity=qty,
                price=price,
                value_cr=round(value_cr, 2),
                category=category,
                deal_date=deal_date,
            )
            session.add(bulk_deal)
            existing_keys.add(key)
            new_count += 1

            if category in (DealCategory.FII, DealCategory.DII):
                institutional_count += 1

    logger.info(
        f"Stored {new_count} new deals ({institutional_count} institutional), "
        f"skipped {skipped} duplicates"
    )
    return institutional_count


async def fetch_and_store_deals() -> int:
    """
    Fetch today's bulk/block deals from NSE, classify clients, and persist to DB.

    Returns the count of institutional (FII + DII) deals found.
    """
    bulk_deals, block_deals = await fetch_bulk_block_deals()

    all_deals = []
    for deal in bulk_deals:
        deal["deal_type"] = "BULK"
        all_deals.append(deal)
    for deal in block_deals:
        deal["deal_type"] = "BLOCK"
        all_deals.append(deal)

    return await _store_deals(all_deals)


async def backfill_deals(days: int = 30) -> int:
    """
    Fetch all available bulk/block deals from NSE snapshot API.

    The NSE snapshot endpoint contains deals from recent trading days.
    Historical API endpoints are restricted, so this is the best we can do.
    Existing deals are automatically deduplicated.
    """
    logger.info(f"Backfilling deals from NSE snapshot...")

    try:
        from nsepython import nsefetch

        url = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
        data = await asyncio.to_thread(nsefetch, url)

        all_deals = []

        # BULK_DEALS_DATA and BLOCK_DEALS_DATA contain structured deal dicts
        # (BULK_DEALS and BLOCK_DEALS are string values, not lists)
        # SHORT_DEALS_DATA is excluded as it lacks clientName/buySell data
        for deal in data.get("BULK_DEALS_DATA", []):
            deal["deal_type"] = "BULK"
            all_deals.append(deal)
        for deal in data.get("BLOCK_DEALS_DATA", []):
            deal["deal_type"] = "BLOCK"
            all_deals.append(deal)

        logger.info(f"Snapshot contains {len(all_deals)} total deals")
        return await _store_deals(all_deals)

    except ImportError:
        logger.error("nsepython not installed — cannot backfill deals")
        return 0
    except Exception as e:
        logger.error(f"Failed to backfill deals: {e}")
        return 0
