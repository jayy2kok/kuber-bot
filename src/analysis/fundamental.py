"""
Fundamental Analysis Engine — GARP scoring for long-term investment.

Ported from: external/long_term_scanner.py → get_fundamentals() + fundamental scoring
Reference: Plan.md Section 5A.1

Responsibilities:
  - Extract fundamental metrics from stored data
  - Apply hard filters (gate checks)
  - Calculate fundamental score (0-70 scale)
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src.data.stock_universe import get_industry_pe_median

logger = logging.getLogger(__name__)


# ─── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class FundamentalMetrics:
    """Extracted fundamental metrics for a single stock."""
    cmp: float                  # Current market price
    market_cap_cr: float        # Market cap in ₹ Crores
    roe_pct: float              # Return on equity %
    de_ratio: float             # Debt to equity ratio
    pe: float                   # P/E ratio (trailing or forward)
    industry_pe: float          # Industry median P/E
    eps_growth: float           # EPS growth %
    rev_growth: float           # Revenue growth %
    promoter_pct: float         # Promoter / insider holding %
    fii_pct: float              # FII / institutional holding %


@dataclass
class FundamentalResult:
    """Result of fundamental analysis for a stock."""
    metrics: FundamentalMetrics
    score: int                  # 0-70 fundamental score
    passed_hard_filters: bool   # Did it pass all gate checks?
    rejection_reason: Optional[str] = None


# ─── Hard Filters ─────────────────────────────────────────────────────────────


def apply_hard_filters(m: FundamentalMetrics) -> tuple[bool, Optional[str]]:
    """
    Apply gate-check filters. Stock is rejected if any filter fails.

    Filters (relaxed for Yahoo Finance data availability):
      - Market Cap: ≥ ₹500 Cr (no upper cap — include large caps)
      - ROE: ≥ 5% (relaxed; Yahoo often returns None → treated as 0)
      - D/E: ≤ 2.0
      - P/E: > 0
    """
    if m.market_cap_cr < 500:
        return False, f"Market cap ₹{m.market_cap_cr:.0f}Cr below ₹500Cr minimum"

    if m.roe_pct < 5:
        return False, f"ROE {m.roe_pct:.1f}% below 5% minimum"

    if m.de_ratio > 2.0:
        return False, f"D/E ratio {m.de_ratio:.2f} above 2.0 maximum"

    if m.pe <= 0:
        return False, f"P/E {m.pe:.1f} is non-positive (loss-making)"

    return True, None


# ─── Scoring ──────────────────────────────────────────────────────────────────


def calculate_fundamental_score(m: FundamentalMetrics) -> int:
    """
    Calculate the fundamental score (max 70 points).

    Scoring breakdown (from Section 5A.1):
      ROE:           max 10  (>25→10, >20→8, >15→6, >10→3)
      Rev Growth:    max 10  (>25→10, >15→8, >10→6, >5→3)
      D/E Ratio:     max 8   (<0.3→8, <0.5→6, <1.0→4, <1.5→1)
      P/E vs Ind:    max 10  (<0.7×ind→10, <1.0×→8, <1.2×→5, <1.5×→2)
      EPS Growth:    max 10  (>25→10, >18→8, >12→6, >5→3)
      Promoter:      max 8   (>60→8, >50→6, >40→4, >30→2)
      Market Cap:    max 5   (₹1K-10K→5, ₹500-15K→3)
      FII:           max 9   (>20→9, >10→7, >5→5, >0→2)
    """
    score = 0

    # ROE (max 10)
    if m.roe_pct > 25:
        score += 10
    elif m.roe_pct > 20:
        score += 8
    elif m.roe_pct > 15:
        score += 6
    elif m.roe_pct > 10:
        score += 3

    # Revenue Growth (max 10)
    if m.rev_growth > 25:
        score += 10
    elif m.rev_growth > 15:
        score += 8
    elif m.rev_growth > 10:
        score += 6
    elif m.rev_growth > 5:
        score += 3

    # D/E Ratio (max 8) — lower is better
    if m.de_ratio < 0.3:
        score += 8
    elif m.de_ratio < 0.5:
        score += 6
    elif m.de_ratio < 1.0:
        score += 4
    elif m.de_ratio < 1.5:
        score += 1

    # P/E vs Industry (max 10) — lower ratio is better
    if m.pe < m.industry_pe * 0.7:
        score += 10
    elif m.pe < m.industry_pe:
        score += 8
    elif m.pe < m.industry_pe * 1.2:
        score += 5
    elif m.pe < m.industry_pe * 1.5:
        score += 2

    # EPS Growth (max 10)
    if m.eps_growth > 25:
        score += 10
    elif m.eps_growth > 18:
        score += 8
    elif m.eps_growth > 12:
        score += 6
    elif m.eps_growth > 5:
        score += 3

    # Promoter Holding (max 8)
    if m.promoter_pct > 60:
        score += 8
    elif m.promoter_pct > 50:
        score += 6
    elif m.promoter_pct > 40:
        score += 4
    elif m.promoter_pct > 30:
        score += 2

    # Market Cap sweet spot (max 5)
    if 1000 <= m.market_cap_cr <= 10000:
        score += 5
    elif 500 <= m.market_cap_cr <= 15000:
        score += 3

    # FII Holding (max 9)
    if m.fii_pct > 20:
        score += 9
    elif m.fii_pct > 10:
        score += 7
    elif m.fii_pct > 5:
        score += 5
    elif m.fii_pct > 0:
        score += 2

    return score


# ─── Public API ───────────────────────────────────────────────────────────────


def extract_metrics_from_db_fundamental(
    fundamental: "Fundamental",
    cmp: float,
) -> Optional[FundamentalMetrics]:
    """
    Convert a DB Fundamental record into FundamentalMetrics.

    Returns None if required fields are missing.
    """
    # P/E and Market Cap are strictly required
    if fundamental.pe_ratio is None and fundamental.forward_pe is None:
        return None
    if fundamental.market_cap_cr is None:
        return None

    pe = fundamental.pe_ratio or fundamental.forward_pe or 0
    if pe <= 0:
        return None

    # Other fields: treat None as 0 (Yahoo often lacks ROE, holdings, etc.)
    # Compute ROE from EPS/Book Value if not directly available
    roe = fundamental.roe_pct
    if roe is None and fundamental.trailing_eps and fundamental.book_value:
        if fundamental.book_value > 0:
            roe = (fundamental.trailing_eps / fundamental.book_value) * 100

    return FundamentalMetrics(
        cmp=cmp,
        market_cap_cr=fundamental.market_cap_cr or 0,
        roe_pct=roe or 0,
        de_ratio=fundamental.debt_to_equity or 0,
        pe=pe,
        industry_pe=fundamental.industry_pe_median or 25.0,
        eps_growth=fundamental.eps_growth_pct or 0,
        rev_growth=fundamental.revenue_growth_pct or 0,
        promoter_pct=fundamental.promoter_holding_pct or 0,
        fii_pct=fundamental.fii_holding_pct or 0,
    )


def analyze_fundamentals(
    fundamental: "Fundamental",
    cmp: float,
) -> Optional[FundamentalResult]:
    """
    Run full fundamental analysis on a stock.

    1. Extract metrics from DB record
    2. Apply hard filters
    3. Calculate score

    Returns FundamentalResult or None if metrics can't be extracted.
    """
    metrics = extract_metrics_from_db_fundamental(fundamental, cmp)
    if metrics is None:
        return None

    passed, reason = apply_hard_filters(metrics)
    score = calculate_fundamental_score(metrics) if passed else 0

    return FundamentalResult(
        metrics=metrics,
        score=score,
        passed_hard_filters=passed,
        rejection_reason=reason,
    )
