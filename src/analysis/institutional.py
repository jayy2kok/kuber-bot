"""
Institutional Activity Analyzer — FII/DII deal aggregation and scoring.

Ported from: external/fii_scanner.py → scan_fii_activity()
Reference: Plan.md Section 5A.2

Responsibilities:
  - Aggregate bulk/block deals per stock from DB
  - Compute net FII and DII buy/sell signals
  - Generate institutional activity score for the composite scorer
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select, and_, func

from src.db.engine import get_session
from src.db.models import BulkDeal, DealCategory

logger = logging.getLogger(__name__)


@dataclass
class InstitutionalSignal:
    """Aggregated institutional activity for a single stock."""
    symbol: str
    # FII
    fii_buy_qty: int = 0
    fii_sell_qty: int = 0
    fii_buy_value_cr: float = 0.0
    fii_sell_value_cr: float = 0.0
    fii_entities: list[str] = field(default_factory=list)
    # DII
    dii_buy_qty: int = 0
    dii_sell_qty: int = 0
    dii_buy_value_cr: float = 0.0
    dii_sell_value_cr: float = 0.0
    dii_entities: list[str] = field(default_factory=list)

    @property
    def fii_net_qty(self) -> int:
        return self.fii_buy_qty - self.fii_sell_qty

    @property
    def fii_net_value_cr(self) -> float:
        return round(self.fii_buy_value_cr - self.fii_sell_value_cr, 2)

    @property
    def dii_net_qty(self) -> int:
        return self.dii_buy_qty - self.dii_sell_qty

    @property
    def dii_net_value_cr(self) -> float:
        return round(self.dii_buy_value_cr - self.dii_sell_value_cr, 2)

    @property
    def fii_signal(self) -> str:
        if self.fii_net_qty > 0:
            return "BUYING"
        elif self.fii_net_qty < 0:
            return "SELLING"
        return "NEUTRAL"

    @property
    def dii_signal(self) -> str:
        if self.dii_net_qty > 0:
            return "BUYING"
        elif self.dii_net_qty < 0:
            return "SELLING"
        return "NEUTRAL"

    @property
    def has_activity(self) -> bool:
        return (
            self.fii_buy_qty + self.fii_sell_qty
            + self.dii_buy_qty + self.dii_sell_qty
        ) > 0


@dataclass
class InstitutionalResult:
    """Result of institutional analysis for a stock."""
    signal: InstitutionalSignal
    score: int  # 0-100 normalized institutional score


async def get_recent_deals(
    symbol: str, lookback_days: int = 30
) -> list[BulkDeal]:
    """Fetch recent institutional deals for a stock from DB."""
    cutoff = date.today() - timedelta(days=lookback_days)
    async with get_session() as session:
        result = await session.execute(
            select(BulkDeal).where(
                and_(
                    BulkDeal.symbol == symbol,
                    BulkDeal.deal_date >= cutoff,
                    BulkDeal.category.in_([
                        DealCategory.FII, DealCategory.DII
                    ]),
                )
            )
        )
        return list(result.scalars().all())


def aggregate_deals(symbol: str, deals: list[BulkDeal]) -> InstitutionalSignal:
    """Aggregate deal data into a per-stock institutional signal."""
    sig = InstitutionalSignal(symbol=symbol)

    for deal in deals:
        if deal.category == DealCategory.FII:
            if deal.buy_sell == "BUY":
                sig.fii_buy_qty += deal.quantity
                sig.fii_buy_value_cr += deal.value_cr
                if deal.client_name not in sig.fii_entities:
                    sig.fii_entities.append(deal.client_name)
            else:
                sig.fii_sell_qty += deal.quantity
                sig.fii_sell_value_cr += deal.value_cr
                if deal.client_name not in sig.fii_entities:
                    sig.fii_entities.append(deal.client_name)

        elif deal.category == DealCategory.DII:
            if deal.buy_sell == "BUY":
                sig.dii_buy_qty += deal.quantity
                sig.dii_buy_value_cr += deal.value_cr
                if deal.client_name not in sig.dii_entities:
                    sig.dii_entities.append(deal.client_name)
            else:
                sig.dii_sell_qty += deal.quantity
                sig.dii_sell_value_cr += deal.value_cr
                if deal.client_name not in sig.dii_entities:
                    sig.dii_entities.append(deal.client_name)

    # Trim to top 5 entities
    sig.fii_entities = sig.fii_entities[:5]
    sig.dii_entities = sig.dii_entities[:5]

    return sig


def calculate_institutional_score(sig: InstitutionalSignal) -> int:
    """
    Calculate institutional activity score (0-100 normalized).

    Scoring logic:
      - FII net buying:  strong positive signal
      - DII net buying:  moderate positive signal
      - Both buying:     strongest signal
      - FII selling:     negative signal
      - No activity:     neutral (50)

    Score breakdown:
      Base:           50 (no activity)
      FII buying:     +15 to +30 based on value
      FII selling:    -15 to -30 based on value
      DII buying:     +5 to +15 based on value
      DII selling:    -5 to -10 based on value
      Multiple entities: +5 bonus
    """
    if not sig.has_activity:
        return 50  # Neutral — no data

    score = 50

    # FII activity (dominant signal)
    fii_net = sig.fii_net_value_cr
    if fii_net > 50:
        score += 30
    elif fii_net > 20:
        score += 25
    elif fii_net > 5:
        score += 20
    elif fii_net > 0:
        score += 15
    elif fii_net < -50:
        score -= 30
    elif fii_net < -20:
        score -= 25
    elif fii_net < -5:
        score -= 20
    elif fii_net < 0:
        score -= 15

    # DII activity (supporting signal)
    dii_net = sig.dii_net_value_cr
    if dii_net > 20:
        score += 15
    elif dii_net > 5:
        score += 10
    elif dii_net > 0:
        score += 5
    elif dii_net < -20:
        score -= 10
    elif dii_net < 0:
        score -= 5

    # Multiple entity bonus
    if len(sig.fii_entities) >= 3:
        score += 5

    return max(0, min(100, score))


async def analyze_institutional(
    symbol: str, lookback_days: int = 30
) -> InstitutionalResult:
    """
    Run institutional activity analysis for a stock.

    1. Fetch recent deals from DB
    2. Aggregate per-stock
    3. Calculate score
    """
    deals = await get_recent_deals(symbol, lookback_days)
    signal = aggregate_deals(symbol, deals)
    score = calculate_institutional_score(signal)

    return InstitutionalResult(signal=signal, score=score)


async def get_all_institutional_signals(
    lookback_days: int = 30,
) -> dict[str, InstitutionalResult]:
    """
    Analyze institutional activity for ALL stocks with recent deals.

    Returns a dict of symbol → InstitutionalResult.
    """
    cutoff = date.today() - timedelta(days=lookback_days)

    async with get_session() as session:
        result = await session.execute(
            select(BulkDeal.symbol).where(
                and_(
                    BulkDeal.deal_date >= cutoff,
                    BulkDeal.category.in_([
                        DealCategory.FII, DealCategory.DII
                    ]),
                )
            ).distinct()
        )
        symbols = [row[0] for row in result.all()]

    results = {}
    for symbol in symbols:
        results[symbol] = await analyze_institutional(symbol, lookback_days)

    logger.info(
        f"Analyzed institutional activity for {len(results)} stocks "
        f"({lookback_days}-day lookback)"
    )
    return results
