"""
Technical Analysis Engine — indicators, scoring, and target calculation.

Ported from: external/long_term_scanner.py → get_technicals() + technical scoring
Reference: Plan.md Section 5A.1
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─── Indicator Calculations ──────────────────────────────────────────────────


def add_emas(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    for p in periods:
        df[f"EMA{p}"] = df["Close"].ewm(span=p, adjust=False).mean()
    return df


def add_sma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    for p in periods:
        df[f"SMA{p}"] = df["Close"].rolling(window=p).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - df["Close"].shift(1)).abs()
    tr3 = (df["Low"] - df["Close"].shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR"] = true_range.ewm(span=period, adjust=False).mean()
    return df


def add_ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    h9, l9 = df["High"].rolling(9).max(), df["Low"].rolling(9).min()
    tenkan = (h9 + l9) / 2
    h26, l26 = df["High"].rolling(26).max(), df["Low"].rolling(26).min()
    kijun = (h26 + l26) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    h52, l52 = df["High"].rolling(52).max(), df["Low"].rolling(52).min()
    span_b = ((h52 + l52) / 2).shift(26)
    df["Ichimoku_Cloud_Top"] = pd.concat([span_a, span_b], axis=1).max(axis=1)
    return df


def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    df["Return_6M"] = ((c / c.shift(126)) - 1) * 100 if len(df) >= 126 else np.nan
    df["Return_1Y"] = ((c / c.shift(252)) - 1) * 100 if len(df) >= 252 else np.nan
    return df


def add_macd(df: pd.DataFrame) -> pd.DataFrame:
    e12 = df["Close"].ewm(span=12, adjust=False).mean()
    e26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = e12 - e26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
    return df


def add_bollinger(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    sma = df["Close"].rolling(period).mean()
    std = df["Close"].rolling(period).std()
    df["BB_Upper"] = sma + 2 * std
    df["BB_Lower"] = sma - 2 * std
    return df


# ─── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class TechnicalMetrics:
    close: float
    ema50: float
    ema200: float
    rsi: float
    atr: float
    ret_6m: float
    ret_1y: float
    above_cloud: bool


@dataclass
class TargetCalculation:
    entry: float
    target_1: float
    target_2: float
    stop_loss: float
    risk_reward: float


@dataclass
class TechnicalResult:
    metrics: TechnicalMetrics
    targets: TargetCalculation
    score: int
    passed_hard_filter: bool
    rejection_reason: Optional[str] = None


# ─── Core Logic ───────────────────────────────────────────────────────────────


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all technical indicators to OHLCV DataFrame."""
    df = df.copy()
    df = add_emas(df, [50, 200])
    df = add_sma(df, [50, 200])
    df = add_rsi(df)
    df = add_atr(df)
    df = add_ichimoku(df)
    df = add_returns(df)
    df = add_macd(df)
    df = add_bollinger(df)
    return df


def extract_technical_metrics(df: pd.DataFrame) -> Optional[TechnicalMetrics]:
    """Extract metrics from the last row of indicator-enriched DataFrame."""
    try:
        last = df.iloc[-1]
        cloud_top = last.get("Ichimoku_Cloud_Top", np.nan)
        close = float(last["Close"])
        r6 = last.get("Return_6M", np.nan)
        r1 = last.get("Return_1Y", np.nan)
        return TechnicalMetrics(
            close=close,
            ema50=float(last["EMA50"]),
            ema200=float(last["EMA200"]),
            rsi=float(last["RSI"]),
            atr=float(last["ATR"]),
            ret_6m=float(r6) if not pd.isna(r6) else 0.0,
            ret_1y=float(r1) if not pd.isna(r1) else 0.0,
            above_cloud=close > cloud_top if not pd.isna(cloud_top) else False,
        )
    except Exception as e:
        logger.warning(f"Failed to extract technical metrics: {e}")
        return None


def apply_hard_filter(m: TechnicalMetrics) -> tuple[bool, Optional[str]]:
    """Hard filter: Price must be above EMA200."""
    if m.close < m.ema200:
        return False, f"Price {m.close:.2f} below EMA200 {m.ema200:.2f}"
    return True, None


def calculate_technical_score(m: TechnicalMetrics) -> int:
    """
    Technical score (max 30 points) — Section 5A.1.

    EMA Alignment: max 10, RSI: max 6, 6M Return: max 8, Ichimoku: max 6
    """
    score = 0
    # EMA alignment (max 10)
    if m.close > m.ema50 > m.ema200:
        score += 10
    elif m.close > m.ema200:
        score += 5
    # RSI 45-70 (max 6)
    if 45 <= m.rsi <= 70:
        score += 6
    elif 40 <= m.rsi <= 75:
        score += 3
    # 6M return (max 8)
    if m.ret_6m > 30:
        score += 8
    elif m.ret_6m > 20:
        score += 6
    elif m.ret_6m > 15:
        score += 4
    elif m.ret_6m > 10:
        score += 2
    # Ichimoku cloud (max 6)
    if m.above_cloud:
        score += 6
    return score


def calculate_targets(m: TechnicalMetrics, cmp: float) -> TargetCalculation:
    """
    Entry/Target/SL — Section 5A.1.

    Entry=min(CMP, EMA50×1.02), T1=+20%, T2=+35%, SL=max(EMA200×0.97, Entry×0.85)
    """
    entry = round(min(cmp, m.ema50 * 1.02), 2)
    if entry > cmp * 1.01:
        entry = round(cmp, 2)
    target_1 = round(entry * 1.20, 2)
    target_2 = round(entry * 1.35, 2)
    ema200_sl = round(m.ema200 * 0.97, 2)
    pct_sl = round(entry * 0.85, 2)
    stop_loss = round(max(ema200_sl, pct_sl), 2)
    risk = abs(entry - stop_loss)
    reward = abs(target_1 - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    return TargetCalculation(entry=entry, target_1=target_1, target_2=target_2,
                             stop_loss=stop_loss, risk_reward=rr)


# ─── Public API ───────────────────────────────────────────────────────────────


def analyze_technicals(df: pd.DataFrame, cmp: float) -> Optional[TechnicalResult]:
    """
    Full technical analysis pipeline.

    1. Compute indicators  2. Extract metrics  3. Hard filter  4. Score  5. Targets
    """
    if df is None or len(df) < 200:
        return None
    df_enriched = compute_all_indicators(df)
    metrics = extract_technical_metrics(df_enriched)
    if metrics is None:
        return None
    passed, reason = apply_hard_filter(metrics)
    score = calculate_technical_score(metrics) if passed else 0
    targets = calculate_targets(metrics, cmp)
    return TechnicalResult(metrics=metrics, targets=targets, score=score,
                           passed_hard_filter=passed, rejection_reason=reason)
