"""AI endpoints — Grok-powered chat and insights.

POST /api/v1/ai/chat
    Conversational assistant. Accepts a list of messages and returns Grok's reply.

GET  /api/v1/ai/fleet-insight?strategy_type=ema_crossover&symbol=BTCUSDT
    Returns Grok's narrative summary of what the fleet has learned for a strategy.

GET  /api/v1/ai/status
    Returns whether the Grok AI is available (key configured).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.config import settings
from app.models.user import User
import app.services.grok_analyst as grok_analyst
from app.services.learning import LearningService

router = APIRouter()


# ------------------------------------------------------------------ #
# Schemas
# ------------------------------------------------------------------ #

class ChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    portfolio_context: dict[str, Any] | None = None
    active_agents: list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    reply: str


class FleetInsightResponse(BaseModel):
    strategy_type: str
    symbol: str | None
    insight: str


class StatusResponse(BaseModel):
    grok_available: bool
    model: str


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@router.get("/status", response_model=StatusResponse)
async def ai_status(
    _: User = Depends(get_current_user),
) -> StatusResponse:
    """Returns whether the Grok AI is available."""
    key = getattr(settings, "XAI_API_KEY", "") or ""
    return StatusResponse(
        grok_available=bool(key),
        model="grok-3-mini (analysis) / grok-3 (chat)",
    )


@router.post("/chat", response_model=ChatResponse)
async def ai_chat(
    body: ChatRequest,
    _: User = Depends(get_current_user),
) -> ChatResponse:
    """Grok-powered conversational AI assistant."""
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    reply = await grok_analyst.chat(
        messages,
        portfolio_context=body.portfolio_context,
        active_agents=body.active_agents,
    )
    return ChatResponse(reply=reply)


@router.get("/fleet-insight", response_model=FleetInsightResponse)
async def fleet_insight(
    strategy_type: str = Query(..., description="Strategy type e.g. ema_crossover"),
    symbol: str | None = Query(None, description="Optional symbol filter e.g. BTCUSDT"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> FleetInsightResponse:
    """Grok narrative summary of fleet-wide optimization knowledge."""
    top_obs = await LearningService.fleet_best(
        db, strategy_type=strategy_type, symbol=symbol, limit=10
    )
    obs_dicts = [
        {
            "params": o.params,
            "backtest_score": float(o.backtest_score or 0),
            "realized_pnl": float(o.realized_pnl or 0),
            "realized_trades": int(o.realized_trades or 0),
            "symbol": o.symbol,
            "timeframe": o.timeframe,
        }
        for o in top_obs
    ]
    insight = await grok_analyst.fleet_insight(obs_dicts, strategy_type=strategy_type, symbol=symbol)
    return FleetInsightResponse(
        strategy_type=strategy_type,
        symbol=symbol,
        insight=insight,
    )
