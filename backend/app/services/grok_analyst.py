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
        logger.debug("AI analysing {} via {}", symbol, "GPT" if is_gpt else "Groq")
    except GrokUnavailableError:
        logger.warning("AI unavailable for {} — no GPT or Groq key", symbol)
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

    # Build screener context
    screener_ctx = agent_ctx.get("screener_said", signal.action) if agent_ctx else signal.action
    screener_conf = agent_ctx.get("screener_confidence", signal.confidence) if agent_ctx else signal.confidence
    screener_reason = agent_ctx.get("screener_reason", signal.reason) if agent_ctx else signal.reason

    prompt = f"""You are the PRIMARY AI brain of a trading agent. YOU decide what trades to make.
You are NOT a validator — you are the decision maker. Analyse the market and find opportunities.

=== MARKET DATA ===
Symbol: {symbol} | Timeframe: {timeframe}
{candle_text}
Indicators: {indicator_text}
Volume: {market_context['volume_vs_avg']} of 20-period average ({market_context['volume_trend']})
10-candle range: {market_context['price_range_10_candles_pct']}
Candle indecision (long wicks): {market_context['candle_indecision']}
Recent momentum (5 candles): {market_context['recent_momentum']}

=== STRATEGY HINT (for context only — you make the final call) ===
Strategy screener says: {screener_ctx} (confidence: {screener_conf:.2f})
Screener reason: {screener_reason}
Note: The screener is a simple indicator check. You can AGREE or DISAGREE with it.
You can find opportunities the screener missed, or reject ones it flagged.

=== AGENT STATE ===
{agent_ctx_text}

=== YOUR TASK: FIND TRADING OPPORTUNITIES ===
Analyse the candle data and decide whether to trade. Check:

1. DIRECTION: Which way is price likely to move in the next few candles?
   Look at momentum, EMA positions, recent highs/lows.

2. ENTRY QUALITY: Is now a good entry point? Look for pullbacks to
   support, bounces off EMA, breakouts with momentum. Avoid entering
   in the middle of nowhere.

3. RISK/REWARD: Can you identify a clear stop loss level (recent
   swing low/high) and a target that gives at least 1.5:1 reward?

4. TIMING: Is the move fresh or are we late? Fresh moves with
   momentum are good. Exhausted moves after big candles are bad.

Output a JSON object:
{{
  "action": "<enter_long|enter_short|exit|hold>",
  "confidence": <0.3–0.90>,
  "reason": "<what you see in the market and why you're making this decision>",
  "market_structure": "<trending_up|trending_down|ranging|choppy>",
  "suggested_stop_loss_pct": <float>,
  "suggested_take_profit_pct": <float>
}}

RULES:
- YOU are the brain. The agent executes your decisions and learns from the results.
- Be DECISIVE. If you see a setup, take it. Don't wait for perfection.
- {"SCALPING MODE: Look for quick mean-reversion entries. Tight stops (0.1-0.3%), quick targets (0.2-0.5%). Volume spikes and momentum shifts are your signals. Trade frequently." if timeframe in ("1m", "5m", "15m") else "Look for clean trend entries or clear reversals with good R:R."}
- Protect capital: use tight stop losses, but don't be afraid to enter when the setup is there.
- If the agent has been losing (check win rate), adjust by tightening stops, not by refusing to trade.
- An agent that never trades never learns. Find the opportunities.
"""
    try:
        try:
            result = await client.chat_json(
                [{"role": "user", "content": prompt}],
                system=_TRADING_SYSTEM,
                mini=False,
            )
        except GPTError as gpt_exc:
            if "429" in str(gpt_exc) or "rate limit" in str(gpt_exc).lower():
                try:
                    groq_client = GrokClient()
                    logger.debug("GPT rate-limited for {} — falling back to Groq", symbol)
                    result = await groq_client.chat_json(
                        [{"role": "user", "content": prompt}],
                        system=_TRADING_SYSTEM,
                        mini=False,
                    )
                    is_gpt = False
                except GrokUnavailableError:
                    raise gpt_exc
            else:
                raise
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


async def groq_analyse(
    signal: Signal,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy_type: str,
    strategy_params: dict[str, Any],
    agent_context: dict[str, Any] | None = None,
) -> Signal:
    """Groq does the heavy market analysis (fast + free).

    Returns a Signal with Groq's recommendation. This is the workhorse —
    called on every candidate symbol.
    """
    try:
        client = GrokClient()
    except GrokUnavailableError:
        return signal

    candle_text = _summarise_candles(candles, n=20)
    indicator_text = _indicator_summary(candles)
    agent_ctx = agent_context or {}

    open_positions = agent_ctx.get("open_positions", [])
    open_symbols = agent_ctx.get("open_symbols", [])
    max_concurrent = agent_ctx.get("max_concurrent_trades", 1)
    already_on_this_pair = symbol in open_symbols

    open_pos_text = ""
    if open_positions:
        lines = [
            f"  {p['symbol']} {p['side']} @ {p['entry_price']} → unrealized {p['unrealized_pnl']:+.4f}"
            for p in open_positions
        ]
        open_pos_text = "CURRENTLY OPEN POSITIONS:\n" + "\n".join(lines) + "\n"

    at_capacity = len(open_positions) >= max_concurrent

    prompt = f"""Analyse this market and decide whether to trade.

Symbol: {symbol} | Timeframe: {timeframe}
{candle_text}
Indicators: {indicator_text}
Strategy screener says: {signal.action} (confidence: {signal.confidence:.2f}) — {signal.reason}

Agent performance: win_rate={agent_ctx.get('win_rate_pct', 0):.1f}% | day_pnl={agent_ctx.get('current_day_pnl', 0):+.4f} | loss_streak={agent_ctx.get('loss_streak', 0)}
{open_pos_text}
{"⚠ ALREADY IN POSITION ON THIS PAIR — only output 'exit' or 'hold', not a new entry." if already_on_this_pair else ""}
{"⚠ AT MAX CONCURRENT TRADES — output 'hold' unless this is an exit signal." if at_capacity and not already_on_this_pair else ""}

Look at momentum, EMA positions, price action. Is there a clear entry or exit?
For scalping ({timeframe}): look for quick mean-reversion or momentum entries with tight stops.

Output JSON:
{{"action": "<enter_long|enter_short|exit|hold>", "confidence": <0.3-0.90>, "reason": "<1-2 sentences>", "suggested_stop_loss_pct": <float>, "suggested_take_profit_pct": <float>}}
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_TRADING_SYSTEM,
            mini=True,
        )
        action = result.get("action", "hold")
        if action not in ("enter_long", "enter_short", "exit", "hold"):
            action = "hold"
        confidence = max(0.3, min(0.90, float(result.get("confidence", 0.4))))
        return Signal(
            action=action,
            confidence=confidence,
            reason=f"[Groq] {result.get('reason', '')}",
            suggested_stop_loss_pct=result.get("suggested_stop_loss_pct") or signal.suggested_stop_loss_pct,
            suggested_take_profit_pct=result.get("suggested_take_profit_pct") or signal.suggested_take_profit_pct,
            metadata={**(signal.metadata or {}), "ai_provider": "groq"},
        )
    except GrokError as exc:
        logger.debug("Groq analysis failed for {}: {}", symbol, exc)
        return signal


async def gpt_decide(
    groq_signal: Signal,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    agent_name: str,
    learning_context: dict[str, Any] | None = None,
) -> Signal:
    """GPT makes the final approve/reject decision on Groq's best pick.

    This is a cheap, focused call — GPT only sees the summary, not raw candles.
    Called at most once per agent per tick.
    """
    try:
        client = GPTClient()
    except GPTUnavailableError:
        return groq_signal

    last_5 = candles.tail(5)[["open", "high", "low", "close"]].round(4).to_string()
    ctx = learning_context or {}

    recent_trades = ctx.get("recent_trades", [])
    open_positions = ctx.get("open_positions", [])
    open_symbols = ctx.get("open_symbols", [])
    already_on_this_pair = symbol in open_symbols

    open_pos_text = ""
    if open_positions:
        lines = [
            f"  {p['symbol']} {p['side']} @ {p['entry_price']} unrealized={p['unrealized_pnl']:+.4f}"
            for p in open_positions
        ]
        open_pos_text = "Open positions: " + " | ".join(
            f"{p['symbol']} {p['side']}" for p in open_positions
        )

    recent_text = ""
    if recent_trades:
        lines = []
        for t in recent_trades[:5]:
            status = t.get("status", "")
            pnl = t.get("pnl", 0)
            pnl_str = f"{pnl:+.4f}" if status == "filled" else "STILL OPEN"
            lines.append(f"  {t.get('symbol')} {t.get('side')} → {pnl_str}")
        recent_text = "Recent trades:\n" + "\n".join(lines)

    loss_streak = ctx.get("loss_streak", 0)
    win_rate = ctx.get("win_rate_pct", 0)
    day_pnl = ctx.get("current_day_pnl", 0)
    recovery = ctx.get("recovery_mode", False)

    performance_text = (
        f"Win rate: {win_rate:.1f}% | Day P&L: {day_pnl:+.4f} USDT"
        f" | Loss streak: {loss_streak}"
        + (" | ⚠ RECOVERY MODE" if recovery else "")
    )

    prompt = f"""Groq AI analysed {symbol} on {timeframe} and recommends: {groq_signal.action} (confidence: {groq_signal.confidence:.2f})
Groq's reasoning: {groq_signal.reason}
SL: {groq_signal.suggested_stop_loss_pct}% | TP: {groq_signal.suggested_take_profit_pct}%

Last 5 candles:
{last_5}

Agent: {agent_name}
{performance_text}
{open_pos_text}
{recent_text}
{"⚠ ALREADY IN POSITION ON THIS PAIR — REJECT any new entry." if already_on_this_pair and groq_signal.action in ("enter_long", "enter_short") else ""}

Do you APPROVE or REJECT this trade? Consider:
1. Does the reasoning make sense given the candle data?
2. Is the risk/reward acceptable?
3. Is this a good entry point or are we chasing?
4. Given the recent trade history and loss streak, is now a good time to enter?
   If loss_streak >= 3, require stronger conviction before approving.
5. If there is already an open position on this pair, REJECT a new entry.

Output JSON: {{"decision": "approve" or "reject", "confidence": <0.3-0.90>, "reason": "<1 sentence>"}}
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system="You are a senior trading risk manager. Approve good setups, reject bad ones. Be decisive.",
            max_tokens=200,
        )
        decision = result.get("decision", "reject")
        reason = result.get("reason", "")
        gpt_conf = float(result.get("confidence", 0.5))

        if decision == "approve":
            logger.info("GPT APPROVED {} {} (conf {:.2f}): {}", symbol, groq_signal.action, gpt_conf, reason)
            return Signal(
                action=groq_signal.action,
                confidence=gpt_conf,
                reason=f"[GPT approved] {reason} | [Groq] {groq_signal.reason}",
                suggested_stop_loss_pct=groq_signal.suggested_stop_loss_pct,
                suggested_take_profit_pct=groq_signal.suggested_take_profit_pct,
                metadata={**(groq_signal.metadata or {}), "ai_provider": "gpt+groq", "gpt_decision": "approve"},
            )
        else:
            logger.info("GPT REJECTED {} {} (conf {:.2f}): {}", symbol, groq_signal.action, gpt_conf, reason)
            return Signal("hold", 0.3, f"[GPT rejected] {reason}", metadata={"gpt_decision": "reject"})
    except GPTError as exc:
        logger.warning("GPT decision failed: {} — using Groq signal", exc)
        return groq_signal


async def suggest_params(
    strategy_type: str,
    candles: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    search_space: dict[str, tuple[float, float]],
    existing_warm_starts: list[dict[str, Any]],
    loss_context: list[dict[str, Any]] | None = None,
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

    loss_section = ""
    if loss_context:
        loss_section = f"""
Recent losing trades to learn from (DO NOT suggest params that would repeat these):
{json.dumps(loss_context, indent=2)}

Analyse WHY these trades lost (market regime? wrong direction? too tight SL? too wide deviation
threshold letting in weak signals?) and use that diagnosis to pick params that would AVOID
repeating the same mistakes.
"""

    prompt = f"""Strategy: {strategy_type}  Symbol: {symbol}  Timeframe: {timeframe}

{candle_text}
Indicators: {indicator_text}
{loss_section}
Parameter search space:
{space_text}

Top existing warm-start params (from fleet learning):
{warm_text}

Task: Given the market regime visible in the candles and the losing trade history above,
suggest ONE concrete parameter set that you believe will perform well.
Values MUST fall within the min/max bounds above.
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


async def coach_review(
    metrics: dict[str, Any],
    recent_trades: list[dict[str, Any]],
    *,
    strategy_type: str,
    current_params: dict[str, Any],
    search_space: dict[str, tuple[float, float]],
) -> dict[str, Any] | None:
    """Coach agent: review performance metrics and suggest parameter nudges.

    Called every 2 hours by the coach scheduler job — NOT on every tick.
    Returns a dict of param → float nudges to apply, or None on failure.

    The coach nudges params proactively (before the agent needs to pause)
    based on patterns like: losing in trending regimes, tight SL causing
    too many stops, low win rate, high drawdown.

    Uses GPT as the primary diagnostician (more reliable reasoning over
    structured data); falls back to Groq if GPT is unavailable.
    """
    try:
        client, is_gpt = _get_premium_client()
    except GrokUnavailableError:
        return None

    by_regime = metrics.get("by_regime", {})
    regime_text = ""
    if by_regime:
        lines = []
        for regime, stats in by_regime.items():
            lines.append(
                f"  {regime}: {stats['total']} trades, "
                f"win_rate={stats['win_rate']}%, pnl={stats['pnl']:+.4f}"
            )
        regime_text = "Performance by market regime:\n" + "\n".join(lines)

    space_text = json.dumps(
        {k: {"min": v[0], "max": v[1]} for k, v in search_space.items()}, indent=2
    )
    trades_text = json.dumps(recent_trades[:10], indent=2) if recent_trades else "none"

    prompt = f"""You are the Coach Agent for a crypto trading system.
Review this agent's performance and suggest parameter adjustments.

Strategy: {strategy_type}
Current params: {json.dumps(current_params, indent=2)}

Performance summary (last 30 trades):
  Total trades: {metrics.get('total_trades', 0)}
  Win rate: {metrics.get('win_rate', 0):.1f}%
  Profit factor: {metrics.get('profit_factor', 0):.3f}  (>1.0 = profitable, <1.0 = losing)
  Avg PnL per trade: {metrics.get('avg_pnl', 0):+.4f} USDT
  Max drawdown: {metrics.get('max_drawdown_usdt', 0):.4f} USDT
  Gross profit: {metrics.get('gross_profit', 0):.4f} | Gross loss: {metrics.get('gross_loss', 0):.4f}

{regime_text}

Recent closed trades (newest first):
{trades_text}

Allowed parameter search space:
{space_text}

Diagnose the most impactful issue and suggest ONE focused parameter change.
For example:
- If losing more in trending_up/trending_down than ranging → raise min_confidence or tighten stop_loss
- If profit_factor < 1.0 with many small wins but big losses → tighten stop_loss_pct
- If win_rate < 35% → widen deviation_pct (less frequent but higher-quality entries)
- If win_rate > 60% but avg_pnl is negative → raise profit_target_pct
- If performance looks fine (profit_factor > 1.2, win_rate > 45%) → suggest no change

Return ONLY a JSON object. If no change is needed, return {{"no_change": true, "reason": "<why>"}}.
Otherwise return param name → float pairs, e.g. {{"stop_loss_pct": 0.12, "min_confidence": 0.52}}.
Values MUST be within the search space bounds above.
"""
    try:
        result = await client.chat_json(
            [{"role": "user", "content": prompt}],
            system=_PARAM_SYSTEM,
            mini=True,
        )
        provider = "GPT" if is_gpt else "Groq"
        if result.get("no_change"):
            logger.debug("coach review ({}): no change needed — {}", provider, result.get("reason", ""))
            return None
        logger.info("coach review ({}) suggested nudge: {}", provider, {k: v for k, v in result.items() if k in search_space})
        validated: dict[str, Any] = {}
        for k, (lo, hi) in search_space.items():
            if k in result:
                try:
                    v = float(result[k])
                    validated[k] = round(max(lo, min(hi, v)), 4)
                except (TypeError, ValueError):
                    pass
        return validated if validated else None
    except (GrokError, GPTError) as exc:
        logger.warning("coach review failed: {}", exc)
        return None


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
