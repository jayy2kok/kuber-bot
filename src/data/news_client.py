"""
News data aggregation client.

Fetches news from multiple RSS feeds and optional NewsAPI.
This is the data layer only — sentiment classification happens in analysis/sentiment.py.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional


def _strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a timezone-aware datetime to UTC naive for DB storage."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

import httpx
from sqlalchemy import select

from src.config import get_settings
from src.db.engine import get_session
from src.db.models import NewsArticle

logger = logging.getLogger(__name__)

# ─── RSS Feed Sources ─────────────────────────────────────────────────────────

RSS_FEEDS = {
    "MoneyControl": "https://www.moneycontrol.com/rss/marketreports.xml",
    "EconomicTimes": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "LiveMint": "https://www.livemint.com/rss/markets",
}

# NSE Corporate Actions feed (separate parser due to unique format)
NSE_CORPORATE_ACTIONS_URL = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"


async def fetch_rss_articles(
    feed_name: str, feed_url: str
) -> list[dict]:
    """
    Fetch and parse articles from a single RSS feed.

    Returns a list of dicts with: title, url, source, summary, published_at
    """
    articles = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(feed_url)
            response.raise_for_status()

        # Basic XML parsing — extract <item> elements
        import xml.etree.ElementTree as ET

        root = ET.fromstring(response.text)
        items = root.findall(".//item")

        for item in items[:20]:  # Limit to 20 per feed
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "").strip()
            pub_date_str = item.findtext("pubDate", "")

            if not title or not link:
                continue

            # Parse published date (common RSS formats)
            pub_date = None
            if pub_date_str:
                for fmt in [
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S GMT",
                    "%Y-%m-%dT%H:%M:%S%z",
                ]:
                    try:
                        pub_date = datetime.strptime(pub_date_str.strip(), fmt)
                        break
                    except ValueError:
                        continue

            articles.append({
                "title": title,
                "url": link,
                "source": feed_name,
                "summary": description[:500] if description else None,
                "published_at": pub_date,
            })

    except Exception as e:
        logger.warning(f"Failed to fetch RSS from {feed_name}: {e}")

    return articles


async def fetch_all_rss() -> list[dict]:
    """Fetch articles from all configured RSS feeds concurrently."""
    tasks = [
        fetch_rss_articles(name, url)
        for name, url in RSS_FEEDS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles = []
    for result in results:
        if isinstance(result, list):
            all_articles.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"RSS fetch error: {result}")

    logger.info(f"Fetched {len(all_articles)} articles from {len(RSS_FEEDS)} RSS feeds")
    return all_articles


# ─── Corporate Action Sentiment Mapping ──────────────────────────────────────

# Map corporate action types to sentiment impact for the LLM context
CORPORATE_ACTION_SENTIMENT = {
    "DIVIDEND":          "positive — company rewarding shareholders",
    "INTERIM DIVIDEND":  "positive — mid-year dividend signals strong cash flow",
    "SPECIAL DIVIDEND":  "positive — exceptional payout shows surplus profits",
    "BONUS":             "positive — bonus issue signals management confidence",
    "SPLIT":             "neutral to positive — stock split improves liquidity",
    "RIGHTS":            "neutral — rights issue may dilute but funds growth",
    "BUYBACK":           "positive — buyback signals undervaluation by management",
    "MERGER":            "uncertain — needs analysis of merger terms",
    "DEMERGER":          "uncertain — needs analysis of demerger terms",
    "AMALGAMATION":      "uncertain — needs analysis of terms",
    "AGM":               "neutral — routine annual general meeting",
    "EGM":               "neutral to noteworthy — extraordinary meeting may signal change",
}


def _classify_corporate_action(purpose: str) -> tuple[str, str]:
    """
    Classify a corporate action purpose string.

    Returns (action_type, sentiment_hint).
    """
    purpose_upper = purpose.upper()
    for action_key, sentiment in CORPORATE_ACTION_SENTIMENT.items():
        if action_key in purpose_upper:
            return action_key, sentiment
    return "OTHER", "neutral — unclassified corporate action"


async def fetch_corporate_actions() -> list[dict]:
    """
    Fetch and parse NSE corporate actions RSS feed.

    Returns articles with enriched title/summary that includes
    the action type, amounts, and ex-date for sentiment analysis.

    Example output article:
        title: "BAJFINANCE: DIVIDEND - RS 6 PER SHARE (Ex-Date: 30-Jun-2026)"
        summary: "Corporate Action: DIVIDEND - RS 6 PER SHARE | Face Value: 500 |
                  Record Date: 30-Jun-2026 | Sentiment: positive — company
                  rewarding shareholders"
    """
    articles = []
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FBot/1.0)",
                "Accept": "application/xml, text/xml",
            },
        ) as client:
            response = await client.get(NSE_CORPORATE_ACTIONS_URL)
            response.raise_for_status()

        import xml.etree.ElementTree as ET

        root = ET.fromstring(response.text)
        items = root.findall(".//item")

        for item in items:
            raw_title = item.findtext("title", "").strip()
            description = item.findtext("description", "").strip()
            pub_date_str = item.findtext("pubDate", "").strip()

            if not raw_title or not description:
                continue

            # Extract company name from title: "Bajaj Finance Limited - Ex-Date: 30-Jun-2026"
            company_name = raw_title.split(" - Ex-Date:")[0].strip() if " - Ex-Date:" in raw_title else raw_title

            # Parse description: "SERIES:EQ |PURPOSE:DIVIDEND - RS 6 |FACE VALUE:500 |RECORD DATE:..."
            fields = {}
            for part in description.split("|"):
                part = part.strip()
                if ":" in part:
                    key, _, value = part.partition(":")
                    fields[key.strip().upper()] = value.strip()

            purpose = fields.get("PURPOSE", "")
            face_value = fields.get("FACE VALUE", "")
            record_date = fields.get("RECORD DATE", "")
            ex_date = ""
            if " - Ex-Date:" in raw_title:
                ex_date = raw_title.split(" - Ex-Date:")[1].strip()

            action_type, sentiment_hint = _classify_corporate_action(purpose)

            # Build enriched title and summary for sentiment analysis
            enriched_title = f"{company_name}: {purpose}"
            if ex_date:
                enriched_title += f" (Ex-Date: {ex_date})"

            enriched_summary = (
                f"Corporate Action: {purpose}"
                f" | Face Value: {face_value}"
                f" | Record Date: {record_date}"
                f" | Action Type: {action_type}"
                f" | Sentiment Hint: {sentiment_hint}"
            )

            # Parse published date (NSE format: DD-Mon-YYYY HH:MM:SS)
            pub_date = None
            if pub_date_str:
                for fmt in [
                    "%d-%b-%Y %H:%M:%S",
                    "%d-%B-%Y %H:%M:%S",
                    "%d-%m-%Y %H:%M:%S",
                ]:
                    try:
                        pub_date = datetime.strptime(pub_date_str, fmt)
                        break
                    except ValueError:
                        continue

            # Build a unique URL using company + purpose to avoid dedup issues
            # (NSE RSS uses the same link for all items)
            import hashlib
            url_hash = hashlib.md5(f"{company_name}:{purpose}:{ex_date}".encode()).hexdigest()[:12]
            unique_url = f"https://nsearchives.nseindia.com/corporate-action/{url_hash}"

            articles.append({
                "title": enriched_title,
                "url": unique_url,
                "source": "NSE Corporate Actions",
                "summary": enriched_summary,
                "published_at": pub_date,
                "_company_name": company_name,  # Extra field for symbol matching
            })

        logger.info(f"Fetched {len(articles)} corporate actions from NSE RSS")
    except Exception as e:
        logger.warning(f"Failed to fetch NSE corporate actions RSS: {e}")

    return articles



async def fetch_newsapi_articles(query: str = "Indian stock market") -> list[dict]:
    """
    Fetch articles from NewsAPI.org (optional).

    Returns empty list if NEWS_API_KEY is not configured.
    """
    settings = get_settings()
    if not settings.has_news_api:
        return []

    articles = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": query,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 20,
                    "apiKey": settings.news_api_key,
                },
            )
            response.raise_for_status()
            data = response.json()

        for item in data.get("articles", []):
            pub_at = None
            if item.get("publishedAt"):
                try:
                    pub_at = datetime.fromisoformat(
                        item["publishedAt"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            articles.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": item.get("source", {}).get("name", "NewsAPI"),
                "summary": item.get("description", ""),
                "published_at": pub_at,
            })

    except Exception as e:
        logger.warning(f"NewsAPI fetch failed: {e}")

    return articles


async def _load_stock_lookup() -> dict[str, str]:
    """
    Build a lookup dict for matching news to stocks.

    Returns dict mapping search term (uppercase) → stock symbol.
    Includes: symbol names (e.g. "RELIANCE") and cleaned company
    names (e.g. "RELIANCE INDUSTRIES").
    """
    from src.db.models import Stock

    async with get_session() as session:
        result = await session.execute(
            select(Stock.symbol, Stock.name).where(Stock.is_active == True)  # noqa: E712
        )
        rows = result.all()

    lookup = {}
    # Short names to skip — too generic, would match everything
    skip_names = {
        "THE", "AND", "INDIA", "INDIAN", "LTD", "LIMITED", "PVT",
        "PRIVATE", "INC", "CORP", "TECHNOLOGIES", "INDUSTRIES",
        "INTERNATIONAL", "NATIONAL", "POWER", "ENERGY", "FINANCE",
        "CAPITAL", "GROUP", "GLOBAL", "MAX", "CAN", "YES", "JUST",
    }

    for symbol, name in rows:
        # Always index the raw symbol (e.g. RELIANCE, TCS, INFY)
        if len(symbol) >= 3 and symbol.upper() not in skip_names:
            lookup[symbol.upper()] = symbol

        # Index the company name (first 2-3 significant words)
        # e.g. "Reliance Industries Ltd." → "RELIANCE INDUSTRIES"
        if name:
            words = [
                w for w in name.upper().replace(".", "").split()
                if w not in skip_names and len(w) >= 3
            ]
            if words:
                # Use first significant word if it's unique enough (≥4 chars)
                if len(words[0]) >= 4:
                    lookup[words[0]] = symbol
                # Use first two significant words for better precision
                if len(words) >= 2:
                    lookup[f"{words[0]} {words[1]}"] = symbol

    return lookup


def _match_symbols(text: str, lookup: dict[str, str]) -> list[str]:
    """
    Match a news text against the stock lookup to find mentioned symbols.

    Uses word-boundary matching to avoid false positives.
    Returns deduplicated list of matched symbols.
    """
    import re

    if not text:
        return []

    text_upper = text.upper()
    matched = set()

    # Try multi-word keys first (more specific), then single-word
    # Sort by length descending so longer (more specific) matches win first
    for term, symbol in sorted(lookup.items(), key=lambda x: len(x[0]), reverse=True):
        if symbol in matched:
            continue
        # Word boundary match to avoid partial matches
        # e.g. "TCS" shouldn't match "ETCS" or "TCSN"
        pattern = r'\b' + re.escape(term) + r'\b'
        if re.search(pattern, text_upper):
            matched.add(symbol)
            if len(matched) >= 5:  # Cap at 5 stocks per article
                break

    return list(matched)


async def fetch_and_store_news() -> int:
    """
    Fetch news from all sources, match to stocks, and store in DB.

    Entity extraction: each article's title + summary is matched against
    known stock symbols and company names. Articles matching multiple
    stocks are stored once per matched stock.

    Returns count of new articles stored.
    """
    # Fetch from all sources
    rss_articles = await fetch_all_rss()
    newsapi_articles = await fetch_newsapi_articles()
    corporate_actions = await fetch_corporate_actions()
    all_articles = rss_articles + newsapi_articles + corporate_actions

    if not all_articles:
        return 0

    # Build stock name lookup for entity extraction
    try:
        stock_lookup = await _load_stock_lookup()
        logger.info(f"Loaded {len(stock_lookup)} terms for news-to-stock matching")
    except Exception as e:
        logger.warning(f"Failed to load stock lookup — storing without symbols: {e}")
        stock_lookup = {}

    new_count = 0
    async with get_session() as session:
        # Get existing URLs to avoid duplicates
        result = await session.execute(select(NewsArticle.url))
        existing_urls = {row[0] for row in result.all()}

        for article in all_articles:
            if article["url"] in existing_urls:
                continue

            # Match article to stock symbols
            # Corporate actions have a pre-extracted company name for better matching
            match_text = article["title"]
            if article.get("summary"):
                match_text += " " + article["summary"]
            if article.get("_company_name"):
                match_text += " " + article["_company_name"]

            matched_symbols = _match_symbols(match_text, stock_lookup) if stock_lookup else []

            if matched_symbols:
                # Store one row per matched stock
                for i, sym in enumerate(matched_symbols):
                    # First match uses the original URL, subsequent get a suffix
                    url = article["url"] if i == 0 else f"{article['url']}#stock={sym}"
                    if url in existing_urls:
                        continue

                    news = NewsArticle(
                        title=article["title"],
                        url=url,
                        source=article["source"],
                        summary=article.get("summary"),
                        published_at=_strip_tz(article.get("published_at")),
                        symbol=sym,
                    )
                    session.add(news)
                    existing_urls.add(url)
                    new_count += 1
            else:
                # No stock match — store without symbol (general market news)
                news = NewsArticle(
                    title=article["title"],
                    url=article["url"],
                    source=article["source"],
                    summary=article.get("summary"),
                    published_at=_strip_tz(article.get("published_at")),
                )
                session.add(news)
                new_count += 1

            existing_urls.add(article["url"])

    logger.info(f"Stored {new_count} new news articles (with entity extraction)")
    return new_count
