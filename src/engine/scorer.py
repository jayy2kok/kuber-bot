"""
Composite Scoring System — two-step adaptive scoring pipeline.

Reference: Plan.md P3.1

Step 1: Pre-filter (runs on all 500 stocks, NO sentiment):
  Pre-Score = (0.40 × Fundamental) + (0.30 × Technical) + (0.30 × Institutional)
  → Shortlist candidates with Pre-Score ≥ 50

Step 2: Sentiment enrichment (runs ONLY on shortlisted stocks):
  WITH Sentiment (Ollama available):
    Final Score = (0.35 × Fundamental) + (0.25 × Technical)
                + (0.20 × Sentiment) + (0.20 × Institutional)
  WITHOUT Sentiment:
    Final Score = Pre-Score (unchanged)
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src.db.models import SignalType, HoldingPeriod

logger = logging.getLogger(__name__)

# ─── Scoring Weights ──────────────────────────────────────────────────────────

# Pre-filter weights (no sentiment)
PRE_WEIGHT_FUNDAMENTAL = 0.40
PRE_WEIGHT_TECHNICAL = 0.30
PRE_WEIGHT_INSTITUTIONAL = 0.30

# Final weights (with sentiment)
FINAL_WEIGHT_FUNDAMENTAL = 0.35
FINAL_WEIGHT_TECHNICAL = 0.25
FINAL_WEIGHT_SENTIMENT = 0.20
FINAL_WEIGHT_INSTITUTIONAL = 0.20

# Score thresholds
PRE_SCORE_THRESHOLD = 40
MIN_RECOMMENDATION_SCORE = 50


@dataclass
class CompositeScore:
    """Complete scoring result for a stock."""
    # Raw sub-scores (0-100 normalized)
    fundamental_score: float    # From 0-70 raw → normalized to 0-100
    technical_score: float      # From 0-30 raw → normalized to 0-100
    institutional_score: float  # Already 0-100
    sentiment_score: Optional[float]  # -100 to +100 → normalized to 0-100

    # Computed scores
    pre_score: float            # Step 1 weighted composite
    final_score: float          # Step 2 weighted composite (may equal pre_score)

    # Classification
    signal: SignalType
    holding_period: HoldingPeriod
    has_sentiment: bool


def normalize_fundamental(raw_score: int) -> float:
    """Normalize fundamental score from 0-70 to 0-100."""
    return round((raw_score / 70) * 100, 1)


def normalize_technical(raw_score: int) -> float:
    """Normalize technical score from 0-30 to 0-100."""
    return round((raw_score / 30) * 100, 1)


def normalize_sentiment(raw_score: float) -> float:
    """Normalize sentiment from -100..+100 to 0..100."""
    return round((raw_score + 100) / 2, 1)


def classify_signal(score: float, is_held: bool) -> SignalType:
    """
    Classify score into signal type — Plan.md P3.2.

    Sell signals are ONLY generated for held stocks.
    """
    if score >= 80:
        return SignalType.STRONG_BUY
    elif score >= 65:
        return SignalType.BUY
    elif score >= 40:
        return SignalType.HOLD
    elif score >= 20:
        return SignalType.SELL if is_held else SignalType.HOLD
    else:
        return SignalType.STRONG_SELL if is_held else SignalType.HOLD


def classify_holding_period(
    fundamental_score: float, technical_score: float
) -> HoldingPeriod:
    """
    Classify as medium-term or super long-term — Plan.md P3.6.

    Super long-term: high fundamental score (>70) dominates over technical.
    """
    if fundamental_score > 70 and fundamental_score > technical_score * 1.5:
        return HoldingPeriod.SUPER_LONG_TERM
    return HoldingPeriod.MEDIUM_TERM


def calculate_pre_score(
    fundamental_norm: float,
    technical_norm: float,
    institutional_norm: float,
) -> float:
    """
    Step 1: Pre-filter score (no sentiment).

    Pre-Score = 0.40×Fund + 0.30×Tech + 0.30×Inst
    """
    score = (
        PRE_WEIGHT_FUNDAMENTAL * fundamental_norm
        + PRE_WEIGHT_TECHNICAL * technical_norm
        + PRE_WEIGHT_INSTITUTIONAL * institutional_norm
    )
    return round(score, 1)


def calculate_final_score(
    fundamental_norm: float,
    technical_norm: float,
    institutional_norm: float,
    sentiment_norm: Optional[float],
) -> float:
    """
    Step 2: Final score with optional sentiment enrichment.

    WITH sentiment:  0.35×Fund + 0.25×Tech + 0.20×Sent + 0.20×Inst
    WITHOUT:         same as pre-score
    """
    if sentiment_norm is not None:
        score = (
            FINAL_WEIGHT_FUNDAMENTAL * fundamental_norm
            + FINAL_WEIGHT_TECHNICAL * technical_norm
            + FINAL_WEIGHT_SENTIMENT * sentiment_norm
            + FINAL_WEIGHT_INSTITUTIONAL * institutional_norm
        )
    else:
        score = calculate_pre_score(
            fundamental_norm, technical_norm, institutional_norm
        )
    return round(score, 1)


def compute_composite_score(
    fundamental_raw: int,
    technical_raw: int,
    institutional_score: float,
    sentiment_raw: Optional[float],
    is_held: bool,
) -> CompositeScore:
    """
    Full two-step composite scoring pipeline.

    Args:
        fundamental_raw: 0-70 raw fundamental score
        technical_raw: 0-30 raw technical score
        institutional_score: 0-100 institutional score
        sentiment_raw: -100 to +100 sentiment (None if unavailable)
        is_held: Whether the stock is currently in holdings (enables sell signals)
    """
    # Normalize all scores to 0-100
    fund_norm = normalize_fundamental(fundamental_raw)
    tech_norm = normalize_technical(technical_raw)
    inst_norm = institutional_score

    sent_norm = None
    if sentiment_raw is not None:
        sent_norm = normalize_sentiment(sentiment_raw)

    # Step 1: Pre-score
    pre = calculate_pre_score(fund_norm, tech_norm, inst_norm)

    # Step 2: Final score
    final = calculate_final_score(fund_norm, tech_norm, inst_norm, sent_norm)

    # Classify
    signal = classify_signal(final, is_held)
    holding = classify_holding_period(fund_norm, tech_norm)

    return CompositeScore(
        fundamental_score=fund_norm,
        technical_score=tech_norm,
        institutional_score=inst_norm,
        sentiment_score=sent_norm,
        pre_score=pre,
        final_score=final,
        signal=signal,
        holding_period=holding,
        has_sentiment=sent_norm is not None,
    )
