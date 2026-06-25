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
from app.services.openai_client import GPTClient, GPTError, GPTUnavailableError
from app.services.strategy.base import Signal, SignalAction


def _get_premium_client() -> tuple[GPTClient | GrokClient, bool]:
    """Get the best available AI client. Prefers GPT, falls back to Groq.

    Returns (client, is_gpt) tuple.
    """
    try:
        return GPTClient(), True
    except GPTUnavailableError:
        pass
    try:
        return GrokClient(), False
    except GrokUnavailableError:
        raise GrokUnavailableError("No AI provider configured (need OPENAI_API_KEY or GROQ_API_KEY)")


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
- CAPITAL PRESERVATION IS YOUR #1 PRIORITY. When uncertain, ALWAYS hold. A missed trade is free; a bad trade costs money.
- Only approve entries when multiple indicators align and the risk/reward is clearly favourable (at least 2:1).
- On short timeframes (1m–15m), be EXTRA cautious — noise is high, false signals are common.
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

async def analyse_market(
    signal: Signal,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy_type: str,
    strategy_params: dict[str, Any],
    agent_context: dict[str, Any] | None = None,
) -> Signal:
    """AI-powered deep market analysis — the primary decision maker.

    The strategy screener identifies a *potential* opportunity. This function
    performs the real analysis: market structure, trend quality, volume
    confirmation, risk/reward, and timing. Only approves entries where
    multiple factors align.

    Returns a Signal. Falls back to HOLD (not the original signal) on error
    when the signal is an entry — we never enter without AI approval.
    """
    try:
        client, is_gpt = _get_premium_client()
    except GrokUnavailableError:
        if signal.action in ("enter_long", "enter_short") and signal.confidence < 0.80:
            return Signal("hold", 0.3, "AI unavailable — holding (confidence too low for unvalidated entry)")
        return signal

    candle_text = _summarise_candles(candles, n=30)
    indicator_text = _indicator_summary(candles)
    agent_ctx = agent_context or {}
    agent_ctx_text = json.dumps(agent_ctx, indent=2)

    # Calculate additional market context for the AI
    close = candles["close"]
    volume = candles["volume"]
    recent_close = close.tail(10)
    avg_volume_20 = float(volume.tail(20).mean())
    current_volume = float(volume.iloc[-1])
    vol_ratio = current_volume / max(avg_volume_20, 1e-9)
    price_range_pct = (float(recent_close.max()) - float(recent_close.min())) / float(recent_close.mean()) * 100
    candle_bodies = (close.tail(5) - candles["open"].tail(5)).abs()
    candle_wicks_upper = candles["high"].tail(5) - close.tail(5).combine(candles["open"].tail(5), max)
    candle_wicks_lower = close.tail(5).combine(candles["open"].tail(5), min) - candles["low"].tail(5)
    avg_body = float(candle_bodies.mean())
    avg_wick = float((candle_wicks_upper + candle_wicks_lower).mean())
    indecision = avg_wick > avg_body * 1.5

    market_context = {
        "volume_vs_avg": f"{vol_ratio:.2f}x",
        "volume_trend": "above avg" if vol_ratio > 1.2 else "below avg" if vol_ratio < 0.8 else "average",
        "price_range_10_candles_pct": f"{price_range_pct:.2f}%",
        "candle_indecision": indecision,
        "recent_momentum": "bullish" if float(close.iloc[-1]) > float(close.iloc[-5]) else "bearish",
    }

    prompt = f"""You are the AI brain of a trading agent. Perform a DEEP market analysis before making a decision.

=== MARKET DATA ===
Symbol: {symbol} | Timeframe: {timeframe}
{candle_text}
Indicators: {indicator_text}
Volume: {market_context['volume_vs_avg']} of 20-period average ({market_context['volume_trend']})
10-candle range: {market_context['price_range_10_candles_pct']}
Candle indecision (long wicks): {market_context['candle_indecision']}
Recent momentum (5 candles): {market_context['recent_momentum']}

=== SCREENER SIGNAL (preliminary, not confirmed) ===
The strategy screener flagged: {signal.action} (confidence: {signal.confidence:.2f})
Reason: {signal.reason}

=== AGENT STATE ===
{agent_ctx_text}

=== YOUR ANALYSIS TASK ===
Perform these checks IN ORDER. If any check fails, output "hold":

1. MARKET STRUCTURE: Is the market trending or ranging? Trending markets
   favor trend-following entries. Ranging markets favor mean-reversion.
   Does the strategy type ({strategy_type}) match the current structure?

2. TREND QUALITY: Is the trend strong and clean, or choppy with
   frequent reversals? Only enter in clean trends or clear reversals.

3. VOLUME CONFIRMATION: Is volume supporting the move? Breakouts need
   above-average volume. Reversals need climactic volume followed by
   declining volume. Below-average volume = weak signal.

4. CANDLE QUALITY: Are the recent candles showing conviction (strong
   bodies) or indecision (long wicks, small bodies, dojis)?
   Indecision candles = do NOT enter.

5. TIMING: Is this an early entry or are we chasing? If the move
   already happened 3+ candles ago, it's too late — hold.

6. RISK/REWARD: Calculate if the entry has at least 2:1 reward to
   risk based on the nearest support/resistance levels.

7. AGENT HISTORY: If the agent has been losing, be EXTRA conservative.
   If win rate is below 50%, only approve the strongest setups.

Output a JSON object:
{{
  "action": "<enter_long|enter_short|exit|hold>",
  "confidence": <0.3–0.90>,
  "reason": "<your analysis summary — what you checked and why you decided this>",
  "market_structure": "<trending_up|trending_down|ranging|choppy>",
  "suggested_stop_loss_pct": <float>,
  "suggested_take_profit_pct": <float>
}}

CRITICAL RULES:
- You are the LAST LINE OF DEFENSE. If you approve, money is on the line.
- Approve when at least 4 of checks 1-6 pass and none are strongly negative.
- On {timeframe} timeframe: {"scalping mode — volume and momentum matter most, allow quick entries with tight stops" if timeframe in ("1m", "5m") else "standard strictness — check all factors but allow reasonable setups" if timeframe == "15m" else "standard strictness applies"}.
- Never chase a move that already happened 5+ candles ago.
- For scalping strategies, favor action over caution — tight stops limit downside.
- A missed trade is FREE. A bad trade COSTS MONEY. But never trading also costs opportunity.
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
            mini=False,
        )
        action: SignalAction = result.get("action", "hold")
        if action not in ("enter_long", "enter_short", "exit", "hold"):
            action = "hold"
        confidence = float(result.get("confidence", 0.4))
        confidence = max(0.3, min(0.90, confidence))

        reason = result.get("reason", "")
        structure = result.get("market_structure", "")
        if structure:
            reason = f"[{structure}] {reason}"

        ai_label = "GPT" if is_gpt else "AI"
        return Signal(
            action=action,
            confidence=confidence,
            reason=f"[{ai_label}] {reason}",
            suggested_stop_loss_pct=result.get("suggested_stop_loss_pct") or signal.suggested_stop_loss_pct,
            suggested_take_profit_pct=result.get("suggested_take_profit_pct") or signal.suggested_take_profit_pct,
            metadata={**(signal.metadata or {}), "ai_analysed": True, "ai_provider": "gpt" if is_gpt else "groq", "market_structure": structure},
        )
    except (GrokError, GPTError) as exc:
        logger.warning("AI market analysis failed: {}", exc)
        if signal.action in ("enter_long", "enter_short") and signal.confidence < 0.80:
            return Signal("hold", 0.3, f"AI unavailable ({exc}) — holding (no unvalidated entries)")
        return signal


# Keep backward compatibility
validate_signal = analyse_market


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
        client, is_gpt = _get_premium_client()
    except GrokUnavailableError:
        return "AI is not configured — fleet insights unavailable."

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
    except (GrokError, GPTError) as exc:
        logger.warning("fleet insight failed: {}", exc)
        return "Fleet insight temporarily unavailable."


async def suggest_agent_tweaks(
    agent_data: dict[str, Any],
    recent_trades: list[dict[str, Any]],
    market_summary: str = "",
) -> dict[str, Any]:
    """Analyse an agent's performance and suggest parameter changes.

    Returns a dict with keys: suggestions (list of actionable tweaks),
    risk_assessment (str), and recommended_params (dict or null).
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return {"suggestions": [], "risk_assessment": "AI unavailable", "recommended_params": None}

    trades_text = json.dumps(recent_trades[:15], indent=2) if recent_trades else "No trades yet"
    prompt = f"""Analyse this trading agent and suggest parameter improvements:

Agent config:
{json.dumps(agent_data, indent=2)}

Recent trades (newest first):
{trades_text}

{f"Current market: {market_summary}" if market_summary else ""}

Respond with a JSON object:
{{
  "suggestions": [
    {{
      "param": "<parameter name or general setting>",
      "current": "<current value or description>",
      "recommended": "<recommended value>",
      "reason": "<why this change would help — one sentence>"
    }}
  ],
  "risk_assessment": "<1-2 sentence assessment of this agent's risk exposure>",
  "recommended_params": {{<full recommended strategy_params dict, or null if no changes needed>}},
  "timeframe_advice": "<is the current timeframe appropriate? suggest better if not>"
}}

Guidelines:
- Focus on LOSS PREVENTION first, profit second.
- If the agent has consecutive losses, suggest tighter stops and higher entry thresholds.
- For short timeframes (1m-15m), recommend tighter stop losses (0.5-1%) and modest take profits (1-2%).
- For longer timeframes (1h-4h), allow wider stops (1.5-3%) and bigger targets (3-6%).
- If win rate is below 40%, suggest switching strategy or widening filters.
- Suggest reducing position size if drawdown is high.
- Maximum 4 suggestions, ordered by impact.
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
            mini=False,
        )
        if not isinstance(result.get("suggestions"), list):
            result["suggestions"] = []
        return result
    except GrokError as exc:
        logger.warning("Grok agent tweaks failed: {}", exc)
        return {"suggestions": [], "risk_assessment": "Analysis temporarily unavailable", "recommended_params": None}


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


async def nudge_stuck_agent(
    agent_data: dict[str, Any],
    last_signals: list[str],
    market_summary: str = "",
) -> dict[str, Any] | None:
    """Called by the trading engine when an agent hasn't placed a trade in many ticks.

    Uses Groq (lightweight) to diagnose why and suggest a fix. Returns a dict
    with keys: diagnosis, action, suggested_params, or None on failure.
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return None

    prompt = f"""An AI trading agent has been running but hasn't placed any trades recently.
Diagnose why and suggest what to change.

Agent config:
{json.dumps(agent_data, indent=2)}

Last 20 signal results: {json.dumps(last_signals)}

{f"Market context: {market_summary}" if market_summary else ""}

Respond with a JSON object:
{{
  "diagnosis": "<1-2 sentence explanation of why the agent isn't trading>",
  "action": "<one of: lower_confidence_threshold, widen_entry_criteria, change_timeframe, switch_strategy, wait_for_setup, none>",
  "suggested_params": {{<specific param changes to make, or empty dict>}},
  "urgency": "<low|medium|high>"
}}

Common causes:
- Confidence threshold too high (min_confidence > 0.65 is very restrictive)
- Wrong timeframe for the strategy (scalping on 4h, trend-following on 1m)
- Market is ranging but strategy needs trending conditions
- Stop loss too tight causing constant rejections
- All signals are "hold" because AI is being too conservative
"""
    try:
        return await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
            mini=True,
        )
    except GrokError as exc:
        logger.warning("stuck agent nudge failed: {}", exc)
        return None
