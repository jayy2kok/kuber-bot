"""
Recommendation Generator — orchestrates the full scan pipeline.

Reference: Plan.md Phase 3

Pipeline:
  1. Load all active stocks
  2. For each stock: run fundamental + technical analysis (hard filters first)
  3. Run institutional analysis (batch)
  4. Step 1 scoring: Pre-Score with 40/30/30 weights → shortlist top candidates
  5. Step 2: Run sentiment ONLY on shortlisted stocks
  6. Final scoring and signal classification
  7. Apply daily limits: top 10 buy, unlimited sell for held stocks
  8. Store recommendations in DB
"""

import asyncio
import logging
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import select, and_

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import (
    Stock, StockPrice, Fundamental, Holding, Recommendation,
    SignalType, HoldingPeriod,
)
from src.analysis.fundamental import analyze_fundamentals
from src.analysis.technical import analyze_technicals
from src.analysis.institutional import analyze_institutional, get_all_institutional_signals
from src.analysis.sentiment import analyze_sentiment
from src.engine.scorer import (
    compute_composite_score, PRE_SCORE_THRESHOLD, MIN_RECOMMENDATION_SCORE,
)
from src.data.stock_universe import get_all_active_stocks

logger = logging.getLogger(__name__)
settings = get_settings()


async def _get_ohlcv_dataframe(stock_id: int) -> Optional[pd.DataFrame]:
    """Load OHLCV data from DB into a pandas DataFrame."""
    async with get_session() as session:
        result = await session.execute(
            select(StockPrice)
            .where(StockPrice.stock_id == stock_id)
            .order_by(StockPrice.date.asc())
        )
        prices = result.scalars().all()

    if len(prices) < 200:
        return None

    data = [{
        "Date": p.date, "Open": p.open, "High": p.high,
        "Low": p.low, "Close": p.close, "Volume": p.volume,
    } for p in prices]

    return pd.DataFrame(data)


async def _get_latest_fundamental(stock_id: int) -> Optional[Fundamental]:
    """Get the most recent fundamental record for a stock."""
    async with get_session() as session:
        result = await session.execute(
            select(Fundamental)
            .where(Fundamental.stock_id == stock_id)
            .order_by(Fundamental.as_of_date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _get_held_symbols() -> set[str]:
    """Get all currently held stock symbols (paper + live)."""
    async with get_session() as session:
        result = await session.execute(
            select(Holding.symbol).where(Holding.quantity > 0)
        )
        return {row[0] for row in result.all()}


def _generate_template_rationale(
    fund_score: float,
    tech_score: float,
    inst_score: float,
    sent_score: Optional[float],
) -> str:
    """Generate a structured template-based rationale (fallback)."""

    # Fundamental bullets
    fund_lines = []
    if fund_score > 70:
        fund_lines.append("• Strong fundamentals with healthy valuations")
    elif fund_score > 50:
        fund_lines.append("• Reasonable valuation with steady metrics")
    elif fund_score > 30:
        fund_lines.append("• Average valuation, needs improvement")
    else:
        fund_lines.append("• Weak valuation profile, high risk")

    if fund_score > 60:
        fund_lines.append("• Good profitability and growth trajectory")
    elif fund_score > 40:
        fund_lines.append("• Moderate growth, mixed profitability")
    else:
        fund_lines.append("• Low growth, profitability concerns")

    if inst_score > 60:
        fund_lines.append("• High institutional & promoter confidence")
    elif inst_score > 40:
        fund_lines.append("• Moderate promoter & institutional holding")
    else:
        fund_lines.append("• Low institutional interest, ownership risk")

    # Technical bullets
    tech_lines = []
    if tech_score > 70:
        tech_lines.append("• Strong bullish momentum on charts")
    elif tech_score > 50:
        tech_lines.append("• Positive price trend forming")
    elif tech_score > 30:
        tech_lines.append("• Neutral trend, awaiting breakout")
    else:
        tech_lines.append("• Bearish technical setup, caution advised")

    if tech_score > 60:
        tech_lines.append("• Entry near key support levels")
        tech_lines.append("• Favorable risk/reward ratio")
    elif tech_score > 40:
        tech_lines.append("• Entry at consolidation zone")
        tech_lines.append("• Moderate risk/reward positioning")
    else:
        tech_lines.append("• Price below key resistance levels")
        tech_lines.append("• Unfavorable risk/reward setup")

    # News/sentiment bullets
    news_lines = []
    if sent_score is not None:
        if sent_score > 60:
            news_lines.append("• Positive sentiment in recent news")
            news_lines.append("• Favorable media coverage")
            news_lines.append("• Market optimism supports outlook")
        elif sent_score > 40:
            news_lines.append("• Neutral sentiment in news flow")
            news_lines.append("• No major positive/negative triggers")
            news_lines.append("• Sector performance inline with market")
        else:
            news_lines.append("• Negative sentiment in recent news")
            news_lines.append("• Adverse media coverage noted")
            news_lines.append("• Headwinds may weigh on performance")
    else:
        news_lines.append("• No recent news data available")
        news_lines.append("• Sentiment analysis pending")
        news_lines.append("• Monitor for upcoming catalysts")

    return (
        f"FUNDAMENTAL:\n" + "\n".join(fund_lines) + "\n\n"
        f"TECHNICAL:\n" + "\n".join(tech_lines) + "\n\n"
        f"NEWS:\n" + "\n".join(news_lines)
    )


RATIONALE_PROMPT = """You are a senior equity research analyst for Indian stock markets.

Analyze {symbol} ({name}) and provide a structured summary based on these data points:

Signal: {signal} | Composite Score: {composite_score}/100

FUNDAMENTALS (Score: {fund_score}/70):
- P/E: {pe:.1f} (Industry: {ind_pe:.1f})
- ROE: {roe:.1f}%, D/E Ratio: {de:.2f}
- EPS Growth: {eps_g:.1f}%, Revenue Growth: {rev_g:.1f}%
- Promoter Holding: {promoter:.1f}%, FII Holding: {fii:.1f}%
- Market Cap: ₹{mcap:,.0f} Cr

TECHNICALS (Score: {tech_score}/30):
- CMP: ₹{cmp:,.2f}
- Entry: ₹{entry:,.2f} | Target: ₹{target:,.2f} (+{upside:.0f}%)
- Stop Loss: ₹{sl:,.2f} | Risk/Reward: {rr:.2f}

INSTITUTIONAL Score: {inst_score}/100
{sentiment_line}

Respond in EXACTLY this format (3 bullets per section, each bullet under 60 characters):

FUNDAMENTAL:
• <insight about valuation>
• <insight about growth/profitability>
• <insight about ownership/risk>

TECHNICAL:
• <insight about price action/trend>
• <insight about entry/targets>
• <insight about risk/reward setup>

NEWS:
• <insight about market sentiment>
• <insight about sector/industry outlook>
• <insight about catalyst or risk>

Rules: No markdown. No headers other than the 3 section labels. Exactly 3 bullets per section. Be specific to {symbol}, not generic."""


# Suppress httpx INFO logs (logs every HTTP request, very noisy)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Module-level cache for Ollama availability (reset each scan)
_ollama_available: Optional[bool] = None


async def _check_ollama_available() -> bool:
    """
    Check if Ollama is available (cached for the process lifetime).

    Only makes one HTTP call — subsequent calls return the cached result.
    """
    global _ollama_available

    if _ollama_available is not None:
        return _ollama_available

    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            _ollama_available = resp.status_code == 200
            if _ollama_available:
                logger.info(f"🤖 Ollama is available at {settings.ollama_base_url}")
            else:
                logger.warning(f"Ollama returned HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Ollama not reachable: {e}")
        _ollama_available = False

    return _ollama_available


async def _generate_llm_rationale(
    symbol: str,
    name: str,
    signal: SignalType,
    composite_score: float,
    fund_result,
    tech_result,
    inst_score: float,
    sent_score: Optional[float],
    cmp: float,
) -> Optional[str]:
    """
    Generate an LLM-enhanced rationale using Ollama.

    Returns the rationale string, or None if Ollama is unavailable or fails.
    """
    import httpx

    if not settings.has_ollama:
        return None

    # Check Ollama availability (cached per scan via module-level flag)
    if not await _check_ollama_available():
        return None

    m = fund_result.metrics
    t = tech_result.targets

    sentiment_line = ""
    if sent_score is not None:
        label = "positive" if sent_score > 50 else "negative" if sent_score < 30 else "neutral"
        sentiment_line = f"NEWS SENTIMENT: {label} (score: {sent_score:.0f}/100)"

    upside = ((t.target_1 - t.entry) / t.entry * 100) if t.entry else 0

    try:
        prompt = RATIONALE_PROMPT.format(
            symbol=symbol,
            name=name,
            signal=signal.value.upper().replace("_", " "),
            composite_score=composite_score,
            fund_score=fund_result.score,
            pe=m.pe,
            ind_pe=m.industry_pe,
            roe=m.roe_pct,
            de=m.de_ratio,
            eps_g=m.eps_growth,
            rev_g=m.rev_growth,
            promoter=m.promoter_pct,
            fii=m.fii_pct,
            mcap=m.market_cap_cr,
            tech_score=tech_result.score,
            cmp=cmp,
            entry=t.entry,
            target=t.target_1,
            sl=t.stop_loss,
            rr=t.risk_reward,
            upside=upside,
            inst_score=inst_score,
            sentiment_line=sentiment_line,
        )
    except Exception as e:
        logger.warning(f"Failed to format LLM prompt for {symbol}: {e}")
        return None

    try:
        logger.info(f"🤖 Generating AI rationale for {symbol}...")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        rationale = data.get("response", "").strip()

        # Sanity check — must be non-empty and not too long
        if rationale and len(rationale) < 1200:
            logger.info(f"✅ AI rationale for {symbol}: {rationale[:80]}...")
            return rationale
        elif rationale:
            logger.info(f"✅ AI rationale for {symbol} (truncated): {rationale[:80]}...")
            return rationale[:1000] + "..."
        else:
            logger.warning(f"Ollama returned empty response for {symbol}")

    except Exception as e:
        logger.warning(f"Ollama rationale generation failed for {symbol}: {e}")

    return None


async def _generate_rationale(
    symbol: str,
    fund_score: float,
    tech_score: float,
    inst_score: float,
    sent_score: Optional[float],
    signal: SignalType,
    # Additional data for LLM rationale (optional, for backward compat)
    stock_name: str = "",
    composite_score: float = 0,
    fund_result=None,
    tech_result=None,
    cmp: float = 0,
) -> str:
    """
    Generate a recommendation rationale.

    Tries LLM-enhanced rationale via Ollama first, then falls back to
    template-based rationale if Ollama is unavailable or fails.
    """
    # Try LLM rationale if we have the required data
    if fund_result and tech_result and stock_name:
        try:
            llm_rationale = await _generate_llm_rationale(
                symbol=symbol,
                name=stock_name,
                signal=signal,
                composite_score=composite_score,
                fund_result=fund_result,
                tech_result=tech_result,
                inst_score=inst_score,
                sent_score=sent_score,
                cmp=cmp,
            )
            if llm_rationale:
                return llm_rationale
        except Exception as e:
            logger.debug(f"LLM rationale fallback for {symbol}: {e}")

    # Fallback: template-based rationale
    return _generate_template_rationale(fund_score, tech_score, inst_score, sent_score)


# ─── Single Stock Analysis (for /analyze command) ────────────────────────────


async def find_stock_fuzzy(query: str) -> list[Stock]:
    """
    Find stocks by symbol or name using fuzzy matching.

    Matching priority:
      1. Exact symbol match (e.g. "RELIANCE")
      2. Symbol starts-with (e.g. "REL")
      3. Substring match in name (e.g. "reliance" → "Reliance Industries Ltd.")
      4. Fuzzy match — each query word appears somewhere in name

    Returns up to 5 matches sorted by relevance.
    """
    query_upper = query.strip().upper()
    query_lower = query.strip().lower()

    async with get_session() as session:
        # 1. Exact symbol match
        result = await session.execute(
            select(Stock).where(Stock.symbol == query_upper)
        )
        exact = result.scalar_one_or_none()
        if exact:
            return [exact]

        # 2. Symbol starts-with
        result = await session.execute(
            select(Stock).where(Stock.symbol.ilike(f"{query_upper}%")).limit(5)
        )
        prefix_matches = list(result.scalars().all())
        if prefix_matches:
            return prefix_matches

        # 3. Substring in name
        result = await session.execute(
            select(Stock).where(Stock.name.ilike(f"%{query_lower}%")).limit(5)
        )
        name_matches = list(result.scalars().all())
        if name_matches:
            return name_matches

        # 4. Fuzzy: all words must appear in name
        words = query_lower.split()
        if len(words) > 1:
            result = await session.execute(select(Stock))
            all_stocks = list(result.scalars().all())
            fuzzy = [
                s for s in all_stocks
                if all(w in s.name.lower() for w in words)
            ]
            return fuzzy[:5]

    return []


async def analyze_single_stock(stock: Stock) -> Optional[Recommendation]:
    """
    Run the full analysis pipeline on a single stock WITHOUT filters.

    Bypasses all hard filters, score thresholds, and daily limits.
    Always generates LLM-enhanced rationale.

    Returns a Recommendation object (NOT persisted to DB).
    """
    scan_date = date.today()
    logger.info(f"📊 Analyzing single stock: {stock.symbol} ({stock.name})")

    # ── Price data ──
    df = await _get_ohlcv_dataframe(stock.id)
    if df is None or df.empty:
        logger.warning(f"No price data for {stock.symbol}")
        return None

    # Use live Kite LTP if authenticated, otherwise fall back to DB close
    db_close = float(df.iloc[-1]["Close"])
    try:
        from src.trading.kite_client import fetch_ltp
        live_price = await fetch_ltp(stock.symbol)
        cmp = live_price if live_price else db_close
        if live_price:
            logger.info(f"Using live Kite LTP for {stock.symbol}: ₹{cmp:.2f} (DB close: ₹{db_close:.2f})")
    except Exception:
        cmp = db_close

    # ── Fundamental ──
    fundamental = await _get_latest_fundamental(stock.id)
    fund_result = None
    if fundamental:
        fund_result = analyze_fundamentals(fundamental, cmp)

    # ── Technical ──
    tech_result = analyze_technicals(df, cmp)

    # ── Institutional ──
    inst_signals = await get_all_institutional_signals(lookback_days=30)
    inst_data = inst_signals.get(stock.symbol)
    inst_score = inst_data.score if inst_data else 50

    # ── Sentiment ──
    sent_score = None
    try:
        sent_result = await analyze_sentiment(stock.symbol)
        if sent_result and sent_result.is_available and sent_result.article_count > 0:
            sent_score = sent_result.score
    except Exception as e:
        logger.warning(f"Sentiment failed for {stock.symbol}: {e}")

    # ── Composite scoring ──
    held_symbols = await _get_held_symbols()
    is_held = stock.symbol in held_symbols

    composite = compute_composite_score(
        fundamental_raw=fund_result.score if fund_result else 0,
        technical_raw=tech_result.score if tech_result else 0,
        institutional_score=inst_score,
        sentiment_raw=sent_score,
        is_held=is_held,
    )

    # ── Rationale (always try LLM for single-stock analysis) ──
    rationale = _generate_template_rationale(
        composite.fundamental_score,
        composite.technical_score,
        composite.institutional_score,
        composite.sentiment_score,
    )

    if fund_result and tech_result:
        try:
            llm_rationale = await _generate_llm_rationale(
                symbol=stock.symbol,
                name=stock.name,
                signal=composite.signal,
                composite_score=composite.final_score,
                fund_result=fund_result,
                tech_result=tech_result,
                inst_score=inst_score,
                sent_score=sent_score,
                cmp=cmp,
            )
            if llm_rationale:
                rationale = llm_rationale
        except Exception as e:
            logger.warning(f"LLM rationale failed for {stock.symbol}: {e}")

    # ── Build recommendation (NOT persisted) ──
    rec = Recommendation(
        stock_id=stock.id,
        scan_date=scan_date,
        fundamental_score=composite.fundamental_score,
        technical_score=composite.technical_score,
        institutional_score=composite.institutional_score,
        sentiment_score=composite.sentiment_score,
        composite_score=composite.final_score,
        signal=composite.signal,
        holding_period=composite.holding_period,
        cmp=cmp,
        entry_price=tech_result.targets.entry if tech_result else cmp,
        target_1=tech_result.targets.target_1 if tech_result else round(cmp * 1.2, 2),
        target_2=tech_result.targets.target_2 if tech_result else round(cmp * 1.35, 2),
        stop_loss=tech_result.targets.stop_loss if tech_result else round(cmp * 0.85, 2),
        risk_reward=tech_result.targets.risk_reward if tech_result else 0,
        rationale=rationale,
        is_delivered=False,
    )

    logger.info(
        f"✅ Analysis complete for {stock.symbol}: "
        f"Score={composite.final_score:.0f}, Signal={composite.signal.value}"
    )
    return rec


async def run_full_scan() -> list[Recommendation]:
    """
    Execute the complete scan pipeline for all active stocks.

    Returns list of generated Recommendation objects.
    """
    scan_date = date.today()
    logger.info(f"Starting full scan for {scan_date}")

    # ── Step 0: Load data ──
    stocks = await get_all_active_stocks()
    held_symbols = await _get_held_symbols()
    inst_signals = await get_all_institutional_signals(lookback_days=30)

    # Fetch live prices from Kite API (if authenticated)
    live_prices: dict[str, float] = {}
    try:
        from src.trading.kite_client import fetch_ltp_batch
        all_symbols = [s.symbol for s in stocks]
        live_prices = await fetch_ltp_batch(all_symbols)
    except Exception as e:
        logger.warning(f"Kite LTP batch fetch failed, using DB close prices: {e}")

    logger.info(
        f"Scanning {len(stocks)} stocks, {len(held_symbols)} held, "
        f"{len(inst_signals)} with institutional data, "
        f"{len(live_prices)} with live Kite prices"
    )

    # ── Step 1: Pre-filter (fundamental + technical + institutional) ──
    candidates = []

    # Diagnostic counters
    skip_no_fundamental = 0
    skip_no_prices = 0
    skip_fund_hard_filter = 0
    skip_tech_hard_filter = 0
    skip_pre_score = 0
    skip_cmp_entry_deviation = 0
    skip_low_rr = 0

    for stock in stocks:
        try:
            is_held = stock.symbol in held_symbols

            # Price data is required for ALL stocks
            df = await _get_ohlcv_dataframe(stock.id)
            if df is None or df.empty:
                skip_no_prices += 1
                continue

            # Use live Kite LTP if available, otherwise DB close
            db_close = float(df.iloc[-1]["Close"])
            cmp = live_prices.get(stock.symbol, db_close)

            # Fundamental analysis
            fundamental = await _get_latest_fundamental(stock.id)
            if fundamental is None:
                skip_no_fundamental += 1
                if not is_held:
                    continue
                fund_result = None  # Held stocks proceed without fundamentals
            else:
                fund_result = analyze_fundamentals(fundamental, cmp)

            # Fundamental hard filter — bypass for held stocks
            if fund_result is not None and not fund_result.passed_hard_filters:
                skip_fund_hard_filter += 1
                if not is_held:
                    continue
            elif fund_result is None and not is_held:
                skip_fund_hard_filter += 1
                continue

            # Technical analysis — always run
            tech_result = analyze_technicals(df, cmp)
            if tech_result is None or not tech_result.passed_hard_filter:
                skip_tech_hard_filter += 1
                if not is_held:
                    continue

            # Institutional score
            inst_result = inst_signals.get(stock.symbol)
            inst_score = inst_result.score if inst_result else 50

            # Pre-score (no sentiment)
            composite = compute_composite_score(
                fundamental_raw=fund_result.score if fund_result else 0,
                technical_raw=tech_result.score if tech_result else 0,
                institutional_score=inst_score,
                sentiment_raw=None,
                is_held=is_held,
            )

            # Pre-score threshold — bypass for held stocks
            if composite.pre_score < PRE_SCORE_THRESHOLD:
                skip_pre_score += 1
                if not is_held:
                    continue

            # CMP-vs-Entry deviation filter — skip if CMP is too far from entry
            if tech_result and tech_result.targets.entry > 0:
                entry = tech_result.targets.entry
                deviation_pct = abs(cmp - entry) / entry * 100
                if deviation_pct > settings.max_cmp_entry_deviation_pct:
                    skip_cmp_entry_deviation += 1
                    logger.debug(
                        f"Skipping {stock.symbol}: CMP ₹{cmp:.2f} vs Entry ₹{entry:.2f} "
                        f"({deviation_pct:.1f}% deviation > {settings.max_cmp_entry_deviation_pct}%)"
                    )
                    if not is_held:
                        continue

            # Risk/Reward filter — skip if risk outweighs reward
            if tech_result and tech_result.targets.risk_reward < settings.min_risk_reward:
                skip_low_rr += 1
                logger.debug(
                    f"Skipping {stock.symbol}: R:R {tech_result.targets.risk_reward:.2f} "
                    f"< min {settings.min_risk_reward}"
                )
                if not is_held:
                    continue

            candidates.append({
                "stock": stock,
                "fund_result": fund_result,
                "tech_result": tech_result,
                "inst_score": inst_score,
                "composite": composite,
                "is_held": is_held,
                "cmp": cmp,
            })

        except Exception as e:
            logger.error(f"Error scanning {stock.symbol}: {e}")
            continue

    logger.info(
        f"Pre-filter: {len(candidates)} candidates passed (threshold={PRE_SCORE_THRESHOLD}). "
        f"Dropped: no_fundamental={skip_no_fundamental}, no_prices(<200d)={skip_no_prices}, "
        f"fund_hard_filter={skip_fund_hard_filter}, tech_hard_filter={skip_tech_hard_filter}, "
        f"pre_score_low={skip_pre_score}, cmp_entry_deviation={skip_cmp_entry_deviation}, "
        f"low_rr={skip_low_rr}"
    )

    # ── Step 2: Sentiment enrichment (ONLY for shortlisted) ──
    for cand in candidates:
        try:
            sent_result = await analyze_sentiment(cand["stock"].symbol)
            if sent_result and sent_result.is_available and sent_result.article_count > 0:
                # Re-score with sentiment
                cand["composite"] = compute_composite_score(
                    fundamental_raw=cand["fund_result"].score,
                    technical_raw=cand["tech_result"].score,
                    institutional_score=cand["inst_score"],
                    sentiment_raw=sent_result.score,
                    is_held=cand["is_held"],
                )
                cand["sentiment_score"] = sent_result.score
            else:
                cand["sentiment_score"] = None
        except Exception as e:
            logger.warning(f"Sentiment failed for {cand['stock'].symbol}: {e}")
            cand["sentiment_score"] = None

    # ── Step 3: Generate recommendations (template rationale only) ──
    recommendations = []

    for cand in candidates:
        comp = cand["composite"]

        if comp.final_score < MIN_RECOMMENDATION_SCORE:
            # Below min score — skip unless it's a sell for held stock
            if not (cand["is_held"] and comp.signal in (
                SignalType.SELL, SignalType.STRONG_SELL
            )):
                continue

        tech = cand["tech_result"]

        # Use fast template rationale for all candidates
        rationale = _generate_template_rationale(
            comp.fundamental_score,
            comp.technical_score,
            comp.institutional_score,
            comp.sentiment_score,
        )

        rec = Recommendation(
            stock_id=cand["stock"].id,
            scan_date=scan_date,
            fundamental_score=comp.fundamental_score,
            technical_score=comp.technical_score,
            institutional_score=comp.institutional_score,
            sentiment_score=comp.sentiment_score,
            composite_score=comp.final_score,
            signal=comp.signal,
            holding_period=comp.holding_period,
            cmp=cand["cmp"],
            entry_price=tech.targets.entry,
            target_1=tech.targets.target_1,
            target_2=tech.targets.target_2,
            stop_loss=tech.targets.stop_loss,
            risk_reward=tech.targets.risk_reward,
            rationale=rationale,
            is_delivered=False,
        )
        # Stash candidate data for LLM enrichment later
        rec._cand_data = cand  # type: ignore[attr-defined]
        recommendations.append(rec)

    # ── Step 4: Apply daily limits ──
    buy_recs = [r for r in recommendations if r.signal in (
        SignalType.STRONG_BUY, SignalType.BUY
    )]
    sell_recs = [r for r in recommendations if r.signal in (
        SignalType.SELL, SignalType.STRONG_SELL
    )]
    hold_recs = [r for r in recommendations if r.signal == SignalType.HOLD]

    # Sort buys by score descending, take top N
    buy_recs.sort(key=lambda r: r.composite_score, reverse=True)
    buy_to_deliver = buy_recs[:settings.daily_buy_limit]
    buy_to_log = buy_recs[settings.daily_buy_limit:]

    # Mark delivery
    for r in buy_to_deliver:
        r.is_delivered = True
    for r in sell_recs:
        r.is_delivered = True  # No limit on sell recommendations

    # ── Step 4.5: LLM rationale ONLY for delivered recommendations ──
    deliverable = buy_to_deliver + sell_recs
    if deliverable:
        logger.info(
            f"🤖 Generating AI rationale for {len(deliverable)} deliverable recommendations..."
        )
        for rec in deliverable:
            try:
                cand = rec._cand_data  # type: ignore[attr-defined]
                comp = cand["composite"]
                llm_rationale = await _generate_llm_rationale(
                    symbol=cand["stock"].symbol,
                    name=cand["stock"].name,
                    signal=comp.signal,
                    composite_score=comp.final_score,
                    fund_result=cand["fund_result"],
                    tech_result=cand["tech_result"],
                    inst_score=cand["inst_score"],
                    sent_score=cand.get("sentiment_score"),
                    cmp=cand["cmp"],
                )
                if llm_rationale:
                    rec.rationale = llm_rationale
            except Exception as e:
                logger.warning(f"LLM rationale failed for stock_id={rec.stock_id}: {e}")

    # Clean up temporary data before persisting
    for rec in recommendations:
        if hasattr(rec, "_cand_data"):
            del rec._cand_data  # type: ignore[attr-defined]

    # ── Step 5: Persist to DB ──
    all_recs = buy_to_deliver + buy_to_log + sell_recs + hold_recs
    async with get_session() as session:
        for rec in all_recs:
            session.add(rec)

    logger.info(
        f"Scan complete: {len(buy_to_deliver)} buy (delivered), "
        f"{len(buy_to_log)} buy (logged only), "
        f"{len(sell_recs)} sell, {len(hold_recs)} hold"
    )

    # Return only deliverable recommendations
    return buy_to_deliver + sell_recs
