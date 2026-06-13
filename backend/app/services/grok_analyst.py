"""Grok Analyst — the AI brain for NeuralSage.

This module provides four capabilities backed by xAI's Grok model:

1. **Signal validation** (`validate_signal`)
   After a rule-based strategy emits a signal, Grok reviews the market
   context (recent OHLCV, indicators, agent state) and either confirms,
   overrides, or adjusts the signal with a confidence score and reasoning.

2. **Parameter suggestions** (`suggest_params`)
   Before running Bayesian optimization Grok can propose parameter seeds
   tuned to the current market regime, supplementing historical warm starts.

3. **Fleet insight** (`fleet_insight`)
   Given a list of top StrategyObservation rows Grok produces a concise
   narrative summary of what the fleet has collectively learned.

4. **Chat assistant** (`chat`)
   Powers the live AI assistant panel in the UI. Grok is primed with
   trading-specific context so it can answer questions about agents,
   positions, market conditions, and strategy performance.

All methods gracefully degrade when XAI_API_KEY is absent — they return
the original signal / empty suggestions / placeholder text so the rest of
the pipeline is never broken by a missing key.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
from loguru import logger

from app.services.grok_client import GrokClient, GrokError, GrokUnavailableError
from app.services.strategy.base import Signal, SignalAction


# ------------------------------------------------------------------ #
# Candle summariser
# ------------------------------------------------------------------ #

def _summarise_candles(df: pd.DataFrame, n: int = 20) -> str:
    """Return a compact string representation of the last N candles."""
    tail = df.tail(n)[["open", "high", "low", "close", "volume"]].copy()
    tail = tail.round(4)
    rows = [
        f"  {i+1}. O={r.open} H={r.high} L={r.low} C={r.close} V={r.volume:.0f}"
        for i, (_, r) in enumerate(tail.iterrows())
    ]
    last_close = float(df["close"].iloc[-1])
    change_pct = (
        (last_close - float(df["close"].iloc[-n])) / float(df["close"].iloc[-n]) * 100
        if len(df) >= n
        else 0.0
    )
    header = f"Last {n} candles (oldest → newest). Last close: {last_close:.4f}  ({change_pct:+.2f}% over window)\n"
    return header + "\n".join(rows)


def _indicator_summary(df: pd.DataFrame) -> str:
    """Return key indicator values if columns are present."""
    parts: list[str] = []
    for col in ("ema_9", "ema_21", "rsi", "volume_sma", "bb_upper", "bb_lower"):
        if col in df.columns:
            val = df[col].iloc[-1]
            if pd.notna(val):
                parts.append(f"{col}={val:.4f}")
    return ", ".join(parts) if parts else "no indicators pre-computed"


# ------------------------------------------------------------------ #
# System prompts
# ------------------------------------------------------------------ #

_TRADING_SYSTEM = """You are NeuralSage's AI trading analyst powered by Groq/Llama.
You assist a crypto-trading platform by analysing market data and agent performance.

You DO have access to live market data — it is provided in the "Live platform data" section
of each request under the key "live_market_prices". Use it to answer questions about current prices.
Never say you lack access to market data; always check the provided data first.

Rules you must follow:
- Be concise and precise. No waffle.
- When asked for JSON output, return ONLY valid JSON — no markdown fences, no commentary.
- Never invent price levels or statistics not present in the supplied data.
- Risk management is paramount: when uncertain, prefer caution (hold / reduce confidence).
- Monetary values are in USDT unless stated otherwise.
- When the user asks about a coin price, look it up in live_market_prices before responding.
"""

_PARAM_SYSTEM = """You are NeuralSage's strategy parameter advisor powered by Grok.
Your job is to suggest concrete trading parameter values for a given strategy and market context.
Output ONLY a JSON object containing parameter name → float value pairs.
Do not include any explanation outside the JSON.
"""


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #

async def validate_signal(
    signal: Signal,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy_type: str,
    strategy_params: dict[str, Any],
    agent_context: dict[str, Any] | None = None,
) -> Signal:
    """Validate/enhance a rule-based strategy signal with Grok analysis.

    Returns a (possibly modified) Signal. Falls back to the original signal
    on any Grok error.
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return signal  # degrade gracefully

    candle_text = _summarise_candles(candles)
    indicator_text = _indicator_summary(candles)
    agent_ctx_text = json.dumps(agent_context or {}, indent=2)

    prompt = f"""Market context:
Symbol: {symbol}  Timeframe: {timeframe}  Strategy: {strategy_type}
Active params: {json.dumps(strategy_params)}
{candle_text}
Indicators: {indicator_text}
Agent state: {agent_ctx_text}

Rule-based signal:
  action: {signal.action}
  confidence: {signal.confidence:.2f}
  reason: {signal.reason}
  suggested_stop_loss_pct: {signal.suggested_stop_loss_pct}
  suggested_take_profit_pct: {signal.suggested_take_profit_pct}

Task: Review the signal in the context of the price action and indicators above.
Respond with a JSON object:
{{
  "action": "<enter_long|enter_short|exit|hold>",
  "confidence": <0.0–1.0>,
  "reason": "<one-sentence explanation>",
  "suggested_stop_loss_pct": <float or null>,
  "suggested_take_profit_pct": <float or null>
}}

Rules:
- You may keep, tighten, or override the action. Never introduce a new direction without strong evidence.
- If the candle data is inconclusive or contradicts the signal, change action to "hold".
- Keep confidence strictly between 0.3 and 0.95.
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
            mini=True,  # use the faster model for per-tick calls
        )
        action: SignalAction = result.get("action", signal.action)
        if action not in ("enter_long", "enter_short", "exit", "hold"):
            action = signal.action
        confidence = float(result.get("confidence", signal.confidence))
        confidence = max(0.3, min(0.95, confidence))
        return Signal(
            action=action,
            confidence=confidence,
            reason=f"[Grok] {result.get('reason', '')}",
            suggested_stop_loss_pct=result.get("suggested_stop_loss_pct") or signal.suggested_stop_loss_pct,
            suggested_take_profit_pct=result.get("suggested_take_profit_pct") or signal.suggested_take_profit_pct,
            metadata={**(signal.metadata or {}), "grok_validated": True},
        )
    except GrokError as exc:
        logger.warning("Grok signal validation failed (using original): {}", exc)
        return signal


async def suggest_params(
    strategy_type: str,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    search_space: dict[str, tuple[float, float]],
    existing_warm_starts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Ask Grok to propose a parameter set for seeding the Bayesian optimizer.

    Returns a dict of param → float, or None on failure.
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return None

    candle_text = _summarise_candles(candles, n=30)
    indicator_text = _indicator_summary(candles)
    space_text = json.dumps({k: {"min": v[0], "max": v[1]} for k, v in search_space.items()}, indent=2)
    warm_text = json.dumps(existing_warm_starts[:3], indent=2) if existing_warm_starts else "none"

    prompt = f"""Strategy: {strategy_type}  Symbol: {symbol}  Timeframe: {timeframe}

{candle_text}
Indicators: {indicator_text}

Parameter search space:
{space_text}

Top existing warm-start params (from fleet learning):
{warm_text}

Task: Given the market regime visible in the candles, suggest ONE concrete parameter set
that you believe will perform well. Values MUST fall within the min/max bounds above.
Return ONLY a JSON object: {{ "param_name": value, ... }}
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_PARAM_SYSTEM,
            mini=True,
        )
        # Validate that all values are numeric and within bounds
        validated: dict[str, Any] = {}
        for k, (lo, hi) in search_space.items():
            if k in result:
                try:
                    v = float(result[k])
                    validated[k] = max(lo, min(hi, v))
                except (TypeError, ValueError):
                    pass
        return validated if validated else None
    except GrokError as exc:
        logger.warning("Grok param suggestion failed: {}", exc)
        return None


async def fleet_insight(
    observations: list[dict[str, Any]],
    *,
    strategy_type: str,
    symbol: str | None = None,
) -> str:
    """Generate a narrative summary of what the fleet has learned for a strategy.

    Returns a plain-text summary (2–4 sentences).
    """
    if not observations:
        return "No fleet observations available yet."
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return "Grok AI is not configured — fleet insights unavailable."

    obs_text = json.dumps(observations[:10], indent=2)
    scope = f"{strategy_type}" + (f" on {symbol}" if symbol else " (all symbols)")

    prompt = f"""Here are the top fleet-learning observations for {scope}:
{obs_text}

Write a 2–4 sentence summary (plain text, no markdown) covering:
1. Which parameter ranges have consistently produced the best results.
2. Any notable patterns (e.g. tighter stops outperforming, specific volatility windows).
3. A brief recommendation for the next optimization cycle.
"""
    try:
        return await client.chat(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
        )
    except GrokError as exc:
        logger.warning("Grok fleet insight failed: {}", exc)
        return "Fleet insight temporarily unavailable."


async def chat(
    messages: list[dict[str, str]],
    *,
    portfolio_context: dict[str, Any] | None = None,
    active_agents: list[dict[str, Any]] | None = None,
) -> str:
    """General-purpose chat for the AI assistant panel.

    Enriches the system prompt with live portfolio / agent data when provided.
    Returns the assistant's reply as a string.
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return (
            "The AI assistant is not configured. Please add your GROQ_API_KEY "
            "(free at console.groq.com) to the environment to enable it."
        )

    context_lines: list[str] = []
    if portfolio_context:
        context_lines.append(f"Current portfolio: {json.dumps(portfolio_context)}")
    if active_agents:
        context_lines.append(f"Active agents ({len(active_agents)}): {json.dumps(active_agents[:5])}")

    extra_context = "\n".join(context_lines)
    system = _TRADING_SYSTEM
    if extra_context:
        system = system + "\n\nLive platform data:\n" + extra_context

    try:
        return await client.chat(messages, system=system, temperature=0.4, max_tokens=512)
    except GrokError as exc:
        logger.warning("Grok chat failed: {}", exc)
        return "I'm having trouble reaching the AI service right now. Please try again shortly."
