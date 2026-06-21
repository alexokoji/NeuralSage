"""AI endpoints — Groq-powered chat and insights.

POST /api/v1/ai/chat
    Conversational assistant. Accepts a list of messages and returns Groq's reply.

GET  /api/v1/ai/fleet-insight?strategy_type=ema_crossover&symbol=BTCUSDT
    Returns Groq's narrative summary of what the fleet has learned for a strategy.

GET  /api/v1/ai/status
    Returns whether the Groq AI is available (key configured).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.config import settings
from app.models.user import User
import app.services.grok_analyst as grok_analyst
from app.services.learning import LearningService

router = APIRouter()


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


@router.get("/status")
async def ai_status(_: User = Depends(get_current_user)):
    from app.services.grok_client import pool_status
    keys = pool_status()
    return {
        "grok_available": len(keys) > 0,
        "model": "llama-3.3-70b-versatile (chat) / llama-3.1-8b-instant (analysis)",
        "keys": keys,
        "total_keys": len(keys),
        "keys_available": sum(1 for k in keys if k["available"]),
    }


_MARKET_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


async def _live_prices() -> dict[str, Any]:
    """Fetch current prices for top coins. Fails silently."""
    from app.services.market_data import get_public_ticker
    prices: dict[str, Any] = {}
    for sym in _MARKET_SYMBOLS:
        try:
            t = await get_public_ticker(sym)
            prices[sym] = {"price": t["price"], "change_24h_pct": t["change_24h_pct"]}
        except Exception:
            pass
    return prices


@router.post("/chat", response_model=ChatResponse)
async def ai_chat(
    body: ChatRequest,
    _: User = Depends(get_current_user),
) -> ChatResponse:
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # Merge live market prices into the portfolio context so Groq can answer
    # questions about current prices without claiming it has no data.
    portfolio_ctx = dict(body.portfolio_context or {})
    portfolio_ctx["live_market_prices"] = await _live_prices()

    reply = await grok_analyst.chat(
        messages,
        portfolio_context=portfolio_ctx,
        active_agents=body.active_agents,
    )
    return ChatResponse(reply=reply)


@router.get("/agent-suggestions/{agent_id}")
async def agent_suggestions(
    agent_id: str,
    user: User = Depends(get_current_user),
):
    """Get AI-powered suggestions for improving an agent's parameters."""
    import uuid
    from app.models.agent import Agent
    from app.models.trade import Trade

    aid = uuid.UUID(agent_id)
    agent = await Agent.get(aid)
    if not agent or agent.user_id != user.id:
        from fastapi import HTTPException
        raise HTTPException(404, "agent not found")

    try:
        recent_trades = await Trade.find(
            Trade.agent_id == aid,
        ).sort(-Trade.opened_at).limit(15).to_list()
    except Exception:
        recent_trades = []

    trade_dicts = [
        {
            "symbol": t.symbol,
            "side": t.side,
            "status": t.status,
            "pnl": float(t.pnl or 0),
            "pnl_pct": float(t.pnl_pct or 0),
            "entry_price": float(t.entry_price or 0),
            "exit_price": float(t.exit_price or 0) if t.exit_price else None,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in recent_trades
    ]

    agent_data = {
        "name": agent.name,
        "strategy": agent.strategy.type if agent.strategy else None,
        "strategy_params": agent.strategy_params or {},
        "trading_pairs": agent.trading_pairs,
        "timeframe": agent.timeframe,
        "assigned_capital": float(agent.assigned_capital),
        "max_risk_per_trade": float(agent.max_risk_per_trade),
        "max_daily_loss": float(agent.max_daily_loss),
        "total_trades": agent.total_trades,
        "winning_trades": agent.winning_trades,
        "total_pnl": float(agent.total_pnl),
        "current_day_pnl": float(agent.current_day_pnl or 0),
        "confidence_score": float(agent.confidence_score),
        "tick_count": agent.tick_count,
    }

    try:
        result = await grok_analyst.suggest_agent_tweaks(agent_data, trade_dicts)
    except Exception as exc:
        result = {
            "suggestions": [],
            "risk_assessment": f"AI analysis temporarily unavailable: {exc}",
            "recommended_params": None,
            "timeframe_advice": None,
        }
    return {"agent_id": agent_id, **result}


@router.post("/generate-strategy")
async def generate_strategy(
    body: dict[str, Any],
    _: User = Depends(get_current_user),
):
    """Ask AI to generate a custom composite strategy based on user's description.

    Body: { "description": "I want a strategy that...", "risk_level": "low|medium|high", "timeframe": "5m" }
    Returns: { "name": "...", "rules": {...}, "params": {...}, "explanation": "..." }
    """
    from app.services.grok_client import GrokClient, GrokError, GrokUnavailableError
    import json

    description = body.get("description", "")
    risk_level = body.get("risk_level", "medium")
    timeframe = body.get("timeframe", "15m")

    if not description:
        from fastapi import HTTPException
        raise HTTPException(400, "description is required")

    try:
        client = GrokClient()
    except GrokUnavailableError:
        return {"error": "AI not configured — add GROQ_API_KEY"}

    prompt = f"""Create a trading strategy based on this description:
"{description}"

Risk level: {risk_level}
Timeframe: {timeframe}

You must output ONLY a JSON object with this exact structure:
{{
  "name": "<short strategy name, 2-4 words>",
  "description": "<1-2 sentence description of what this strategy does>",
  "rules": {{
    "entry_long": [
      <list of conditions, each is an object with "indicator", "op", "value", and optional params>
    ],
    "entry_short": [
      <list of conditions>
    ],
    "exit_long": [
      <list of conditions>
    ],
    "exit_short": [
      <list of conditions>
    ]
  }},
  "params": {{
    "stop_loss_pct": <float based on risk level: low=0.5-0.8, medium=0.8-1.5, high=1.5-3.0>,
    "take_profit_pct": <float, at least 2x stop_loss for good R:R>,
    "min_confidence": <float 0.55-0.75 based on risk level>,
    "position_size_pct": <float 1.0-3.0 based on risk level>
  }},
  "explanation": "<2-3 sentences explaining the logic and when this strategy works best>"
}}

Available indicators for conditions:
- "rsi": period (default 14), op: "<" or ">", value: number (30=oversold, 70=overbought)
- "ema_cross": fast (default 9), slow (default 21), direction: "bullish" or "bearish"
- "volume": op: ">" with value as multiplier of 20-period average (e.g. 1.5 = 50% above avg)
- "price_change": op: "<" or ">", value: percent change from previous candle
- "atr": period (default 14), op: "<" or ">", value: threshold

Rules:
- Each entry must have 2-4 conditions (AND logic) for filtering quality
- Capital preservation first — default to tighter stops for low risk
- For short timeframes (1m-5m): use tighter stops (0.3-0.8%) and smaller targets
- For longer timeframes (1h-4h): allow wider stops and bigger targets
- Always include RSI as one of the conditions to avoid entering at extremes
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system="You are a quantitative trading strategy designer. Output ONLY valid JSON, no commentary.",
            temperature=0.3,
            max_tokens=1024,
        )
        return result
    except GrokError as exc:
        return {"error": f"AI generation failed: {exc}"}


@router.get("/fleet-insight", response_model=FleetInsightResponse)
async def fleet_insight(
    strategy_type: str = Query(..., description="Strategy type e.g. ema_crossover"),
    symbol: str | None = Query(None, description="Optional symbol filter e.g. BTCUSDT"),
    _: User = Depends(get_current_user),
) -> FleetInsightResponse:
    top_obs = await LearningService.fleet_best(
        strategy_type=strategy_type, symbol=symbol, limit=10
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
