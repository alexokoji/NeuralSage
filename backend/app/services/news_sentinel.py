"""News Sentinel — monitors crypto news and scores per-coin sentiment every 10 minutes.

Data sources (no new API keys required):
  - alternative.me Fear & Greed Index (free, no key)
  - CryptoPanic RSS (basic feed, no key)
  - CoinDesk RSS
  - CoinTelegraph RSS

Uses httpx (already a dependency) for HTTP and stdlib xml.etree.ElementTree for RSS parsing.
Groq scores sentiment; results are cached in NewsSignal MongoDB documents.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from loguru import logger

from app.models.news_signal import NewsSignal
from app.services.grok_client import GrokClient, GrokUnavailableError

_RSS_FEEDS = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("cryptopanic", "https://cryptopanic.com/news/rss/"),
    ("decrypt", "https://decrypt.co/feed"),
]

# All coins we score (base tickers only — no quote currency)
_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "SUI",
    "MATIC", "LINK", "DOT", "LTC", "ARB", "TON", "SHIB", "TRX", "NEAR",
]

_SENTIMENT_SCORES: dict[str, float] = {
    "strongly_bearish": -1.0,
    "bearish": -0.5,
    "neutral": 0.0,
    "bullish": 0.5,
    "strongly_bullish": 1.0,
}

_VALID_SENTIMENTS = set(_SENTIMENT_SCORES.keys())

# Quote currencies to strip when converting a trading pair to a base coin
_QUOTE_CURRENCIES = ("USDT", "BUSD", "USD", "BTC", "ETH", "BNB")


def symbol_to_coin(symbol: str) -> str:
    """Extract base coin from a trading pair symbol. 'BTCUSDT' → 'BTC'."""
    upper = symbol.upper()
    for quote in _QUOTE_CURRENCIES:
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)]
    return upper


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


async def run_news_sentinel() -> None:
    """Fetch crypto news + Fear & Greed, score per-coin sentiment, upsert NewsSignal docs."""
    logger.info("news sentinel: starting run")

    fng_value, fng_label = await _fetch_fear_greed()
    headlines = await _fetch_all_headlines()

    logger.info(
        "news sentinel: {} headlines fetched — Fear & Greed={}/100 ({})",
        len(headlines), fng_value, fng_label,
    )

    # Global market signal — always written even if headlines are empty
    market_sentiment = _fng_to_sentiment(fng_value)
    await _upsert(
        coin="MARKET",
        sentiment=market_sentiment,
        score=_SENTIMENT_SCORES[market_sentiment],
        summary=(
            f"Market Fear & Greed index is {fng_value}/100 ({fng_label}). "
            "This is the macro sentiment backdrop for all coins."
        ),
        key_events=[],
        headline_count=len(headlines),
        fng_value=fng_value,
        fng_label=fng_label,
    )

    if not headlines:
        logger.warning("news sentinel: no headlines — per-coin scoring skipped")
        return

    await _score_and_save_coins(headlines, fng_value, fng_label)
    logger.info("news sentinel: run complete")


# --------------------------------------------------------------------------- #
# Fear & Greed index
# --------------------------------------------------------------------------- #


async def _fetch_fear_greed() -> tuple[int, str]:
    """Returns (value 0-100, label). Falls back to (50, 'Neutral') on error."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.alternative.me/fng/?limit=1")
            r.raise_for_status()
            entry = r.json()["data"][0]
            return int(entry["value"]), str(entry["value_classification"])
    except Exception as exc:
        logger.debug("news sentinel: Fear & Greed fetch failed: {}", exc)
        return 50, "Neutral"


def _fng_to_sentiment(value: int) -> str:
    if value <= 20:
        return "strongly_bearish"
    if value <= 40:
        return "bearish"
    if value <= 60:
        return "neutral"
    if value <= 80:
        return "bullish"
    return "strongly_bullish"


# --------------------------------------------------------------------------- #
# RSS fetching
# --------------------------------------------------------------------------- #


async def _fetch_rss(name: str, url: str, client: httpx.AsyncClient) -> list[str]:
    """Fetch one RSS feed and return a list of 'title. description' strings."""
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        texts: list[str] = []
        for item in items[:15]:
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()[:200]
            if title:
                texts.append(f"{title}. {desc}" if desc else title)
        logger.debug("news sentinel: {} → {} headlines", name, len(texts))
        return texts
    except Exception as exc:
        logger.debug("news sentinel: {} RSS error: {}", name, exc)
        return []


async def _fetch_all_headlines() -> list[str]:
    """Fetch all RSS feeds concurrently."""
    headers = {"User-Agent": "NeuralSage/1.0 (crypto trading platform)"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as c:
        feeds = await asyncio.gather(
            *[_fetch_rss(name, url, c) for name, url in _RSS_FEEDS],
            return_exceptions=True,
        )
    result: list[str] = []
    for feed in feeds:
        if isinstance(feed, list):
            result.extend(feed)
    return result


# --------------------------------------------------------------------------- #
# Groq sentiment scoring
# --------------------------------------------------------------------------- #


async def _score_and_save_coins(
    headlines: list[str],
    fng_value: int,
    fng_label: str,
) -> None:
    """One Groq call to score all major coins, then upsert results."""
    try:
        client = GrokClient()
    except GrokUnavailableError:
        logger.warning("news sentinel: Groq unavailable — saving market-inherited scores")
        await _save_market_inherited(fng_value, fng_label)
        return

    headlines_block = "\n".join(f"- {h}" for h in headlines[:40])
    coins_str = ", ".join(_COINS)
    market_context = _fng_to_sentiment(fng_value).replace("_", " ").title()

    prompt = f"""You are a crypto market news analyst. Score the sentiment for each coin based on these news headlines.

MARKET CONTEXT: Fear & Greed Index = {fng_value}/100 ({fng_label}) — macro sentiment is {market_context}.

NEWS HEADLINES (from CoinDesk, CoinTelegraph, CryptoPanic):
{headlines_block}

For each of these coins: {coins_str}

Score the sentiment based on:
1. Any direct news about that coin in the headlines above
2. If no specific news exists for a coin, inherit the macro market sentiment (Fear & Greed = {fng_label})
3. Regulatory or market-wide news affects all coins (adjust score toward neutral when uncertain)

Return ONLY a valid JSON object. For EACH coin in the list, include:
{{
  "BTC": {{
    "sentiment": "bullish",
    "score": 0.5,
    "summary": "2 sentences about what the news says about BTC, or 'No specific news — market sentiment is {fng_label}' if none",
    "key_events": ["ETF inflow record", "Fed rate cut expected"]
  }},
  "ETH": {{ ... }},
  ...
}}

Sentiment values: "strongly_bearish" (-1.0), "bearish" (-0.5), "neutral" (0.0), "bullish" (0.5), "strongly_bullish" (1.0)
Score should match the sentiment label.
key_events: 0-3 short strings, empty list [] if no relevant events.
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system="You are a crypto news analyst. Output only valid JSON — no markdown, no commentary.",
            mini=True,
        )
    except Exception as exc:
        logger.warning("news sentinel: Groq call failed: {} — using market-inherited scores", exc)
        await _save_market_inherited(fng_value, fng_label)
        return

    market_fallback_sentiment = _fng_to_sentiment(fng_value)
    market_fallback_score = _SENTIMENT_SCORES[market_fallback_sentiment] * 0.5  # dampened

    for coin in _COINS:
        coin_data = result.get(coin) if isinstance(result, dict) else None

        if not isinstance(coin_data, dict):
            await _upsert(
                coin=coin,
                sentiment=market_fallback_sentiment,
                score=market_fallback_score,
                summary=f"No specific news found — inheriting market sentiment ({fng_label}).",
                key_events=[],
                headline_count=0,
                fng_value=fng_value,
                fng_label=fng_label,
            )
            continue

        sentiment = str(coin_data.get("sentiment", "neutral"))
        if sentiment not in _VALID_SENTIMENTS:
            sentiment = "neutral"

        raw_score = coin_data.get("score")
        try:
            score = float(raw_score) if raw_score is not None else _SENTIMENT_SCORES[sentiment]
        except (TypeError, ValueError):
            score = _SENTIMENT_SCORES[sentiment]
        score = max(-1.0, min(1.0, score))

        summary = str(coin_data.get("summary", ""))[:500]
        key_events = [
            str(e)[:100]
            for e in (coin_data.get("key_events") or [])[:3]
        ]
        # Count direct mentions in headlines
        headline_count = sum(
            1 for h in headlines
            if coin.lower() in h.lower() or coin.upper() in h.upper()
        )

        await _upsert(
            coin=coin,
            sentiment=sentiment,
            score=score,
            summary=summary,
            key_events=key_events,
            headline_count=headline_count,
            fng_value=fng_value,
            fng_label=fng_label,
        )
        logger.debug(
            "news sentinel: {} → {} (score={:+.2f}, {} headlines)",
            coin, sentiment, score, headline_count,
        )


async def _save_market_inherited(fng_value: int, fng_label: str) -> None:
    """Save all coins with market-inherited sentiment (used when Groq is unavailable)."""
    sentiment = _fng_to_sentiment(fng_value)
    score = _SENTIMENT_SCORES[sentiment] * 0.5
    for coin in _COINS:
        await _upsert(
            coin=coin,
            sentiment=sentiment,
            score=score,
            summary=f"AI scoring unavailable — inheriting market sentiment ({fng_label}).",
            key_events=[],
            headline_count=0,
            fng_value=fng_value,
            fng_label=fng_label,
        )


# --------------------------------------------------------------------------- #
# DB upsert
# --------------------------------------------------------------------------- #


async def _upsert(
    *,
    coin: str,
    sentiment: str,
    score: float,
    summary: str,
    key_events: list[str],
    headline_count: int,
    fng_value: int | None,
    fng_label: str,
) -> None:
    now = datetime.now(timezone.utc)
    existing = await NewsSignal.find_one(NewsSignal.coin == coin)
    if existing:
        existing.sentiment = sentiment
        existing.score = score
        existing.summary = summary
        existing.key_events = key_events
        existing.headline_count = headline_count
        existing.fear_greed_value = fng_value
        existing.fear_greed_label = fng_label
        existing.updated_at = now
        await existing.save()
    else:
        await NewsSignal(
            coin=coin,
            sentiment=sentiment,
            score=score,
            summary=summary,
            key_events=key_events,
            headline_count=headline_count,
            fear_greed_value=fng_value,
            fear_greed_label=fng_label,
            updated_at=now,
        ).insert()
