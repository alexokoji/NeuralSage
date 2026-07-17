"""News sentiment signal — cached per coin, updated every 10 minutes by the news sentinel."""
from __future__ import annotations

from datetime import datetime, timezone

from beanie import Document
from pydantic import Field


class NewsSignal(Document):
    """Latest sentiment snapshot for one coin (or "MARKET" for the global index)."""

    coin: str                       # "BTC", "ETH", "SUI", "MARKET", …
    sentiment: str = "neutral"      # strongly_bearish | bearish | neutral | bullish | strongly_bullish
    score: float = 0.0              # -1.0 (strongly bearish) → +1.0 (strongly bullish)
    summary: str = ""               # 2-3 sentence human-readable summary from Groq
    key_events: list[str] = Field(default_factory=list)   # ["ETF approved", "SEC lawsuit"]
    headline_count: int = 0
    fear_greed_value: int | None = None   # 0–100 from alternative.me
    fear_greed_label: str = ""            # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "news_signals"
