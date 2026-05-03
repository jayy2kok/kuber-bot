"""
Sentiment Analysis Engine — optional Ollama-based news sentiment scoring.

Reference: Plan.md P2.3

Key design constraint (from plan):
  ⚠️ Sentiment analysis is NOT run on all 500 stocks. It is triggered ONLY
  for stocks that have already passed fundamental + technical + institutional
  screening (typically ~10-30 shortlisted candidates).

Responsibilities:
  - Fetch recent news for a specific stock symbol
  - Classify sentiment using Ollama (local LLM) when available
  - Graceful degradation: returns None if Ollama is unavailable
  - Sentiment scoring per stock (-100 to +100)
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import select, and_

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import NewsArticle

logger = logging.getLogger(__name__)

SENTIMENT_PROMPT_TEMPLATE = """You are a financial news sentiment analyzer for Indian stock markets.

Analyze the following news headlines/summaries related to the stock "{symbol}" and classify the overall sentiment.

News items:
{news_text}

Respond with ONLY a JSON object in this exact format (no other text):
{{"score": <integer from -100 to 100>, "label": "<positive|negative|neutral>", "reason": "<one sentence explanation>"}}

Score guide:
  -100 to -60: Very negative (fraud, bankruptcy, regulatory action)
  -60 to -20: Negative (earnings miss, downgrade, management exit)
  -20 to 20: Neutral (routine updates, mixed signals)
  20 to 60: Positive (earnings beat, expansion, upgrade)
  60 to 100: Very positive (major contract, breakthrough, strong guidance)
"""


@dataclass
class SentimentResult:
    """Result of sentiment analysis for a stock."""
    symbol: str
    score: float            # -100 to +100
    label: str              # positive, negative, neutral
    reason: str             # One-line explanation
    article_count: int      # Number of articles analyzed
    is_available: bool      # Was Ollama actually used?
_ollama_available_cached: Optional[bool] = None


async def check_ollama_available() -> bool:
    """Check if Ollama server is reachable (cached after first call)."""
    global _ollama_available_cached

    if _ollama_available_cached is not None:
        return _ollama_available_cached

    settings = get_settings()
    if not settings.has_ollama:
        _ollama_available_cached = False
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            _ollama_available_cached = resp.status_code == 200
    except Exception:
        _ollama_available_cached = False

    if _ollama_available_cached:
        logger.info("Ollama available for sentiment analysis")
    else:
        logger.warning("Ollama not available for sentiment analysis")

    return _ollama_available_cached


async def get_recent_news_for_stock(
    symbol: str, days: int = 7
) -> list[NewsArticle]:
    """Fetch recent news articles linked to a stock symbol from DB."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with get_session() as session:
        result = await session.execute(
            select(NewsArticle)
            .where(
                and_(
                    NewsArticle.symbol == symbol,
                    NewsArticle.published_at >= cutoff,
                )
            )
            .order_by(NewsArticle.published_at.desc())
            .limit(10)
        )
        return list(result.scalars().all())


async def classify_sentiment_ollama(
    symbol: str, news_texts: list[str]
) -> Optional[dict]:
    """
    Call Ollama to classify sentiment for a stock's news.

    Returns dict with keys: score, label, reason — or None on failure.
    """
    settings = get_settings()
    news_block = "\n".join(f"- {t}" for t in news_texts[:10])

    prompt = SENTIMENT_PROMPT_TEMPLATE.format(
        symbol=symbol, news_text=news_block
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        response_text = data.get("response", "")

        import json
        result = json.loads(response_text)

        score = max(-100, min(100, int(result.get("score", 0))))
        label = result.get("label", "neutral")
        reason = result.get("reason", "No explanation provided")

        return {"score": score, "label": label, "reason": reason}

    except Exception as e:
        logger.warning(f"Ollama sentiment call failed for {symbol}: {e}")
        return None


async def analyze_sentiment(symbol: str) -> Optional[SentimentResult]:
    """
    Run sentiment analysis for a single stock.

    This is called ONLY for shortlisted stocks (post fundamental+technical
    scoring). Returns None if:
      - Ollama is unavailable (graceful degradation)
      - No recent news found for the stock
    """
    # Check Ollama availability
    available = await check_ollama_available()
    if not available:
        logger.debug(f"Ollama unavailable — skipping sentiment for {symbol}")
        return SentimentResult(
            symbol=symbol, score=0, label="neutral",
            reason="Ollama unavailable — sentiment skipped",
            article_count=0, is_available=False,
        )

    # Fetch recent news
    articles = await get_recent_news_for_stock(symbol)
    if not articles:
        return SentimentResult(
            symbol=symbol, score=0, label="neutral",
            reason="No recent news found",
            article_count=0, is_available=True,
        )

    # Build news texts
    news_texts = []
    for a in articles:
        text = a.title
        if a.summary:
            text += f" — {a.summary[:200]}"
        news_texts.append(text)

    # Classify with Ollama
    result = await classify_sentiment_ollama(symbol, news_texts)
    if result is None:
        return SentimentResult(
            symbol=symbol, score=0, label="neutral",
            reason="Ollama classification failed",
            article_count=len(articles), is_available=True,
        )

    # Update sentiment scores in DB
    async with get_session() as session:
        for article in articles:
            if article.sentiment_score is None:
                article.sentiment_score = result["score"]
                article.sentiment_label = result["label"]
                session.add(article)

    return SentimentResult(
        symbol=symbol,
        score=result["score"],
        label=result["label"],
        reason=result["reason"],
        article_count=len(articles),
        is_available=True,
    )
