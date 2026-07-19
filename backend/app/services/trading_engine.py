"""Trading Engine — MongoDB/Beanie edition.

Pipeline for one tick of one agent:
  1. Pull candles from the agent's exchange.
  2. Ask the strategy for a signal.
  3. If the signal is enter/exit:
     a. Run RiskEngine.evaluate_entry (entries only).
     b. Place the order via the exchange client.
     c. Persist a Trade doc + (for entries) a Position doc.
     d. Update agent counters and emit a notification.
  4. If no actionable signal, persist nothing.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.trade import Trade
from app.services.exchange import OrderRequest, build_client
from app.services.exchange.base import ExchangeError, OrderResult
import app.services.grok_analyst as grok_analyst
from app.services.notifications import NotificationService
from app.services.risk_engine import RiskEngine
from app.services.signal_policy import (
    get_entry_confidence_threshold,
    should_execute_entry_signal,
    should_prefer_screener,
)
from app.services.strategy import StrategyContext, get_strategy
from app.services.strategy.indicators import candles_to_df


class TradingEngine:
    """One-shot per-agent execution. Designed for the scheduler tick."""
    # Agents with assigned capital at or below this threshold will receive
    # conservative automatic defaults so tiny accounts can still place trades.
    _SMALL_ACCOUNT_CAP_THRESHOLD = 50.0  # USD
    _SMALL_ACCOUNT_DEFAULT_SL = 0.5  # percent
    _SMALL_ACCOUNT_DEFAULT_MAX_RISK_PCT = 5.0  # percent

    async def run_agent_tick(self, agent: Agent, api_key: ApiKey) -> dict[str, Any]:
        if agent.status != "active":
            return {"skipped": True, "reason": f"agent {agent.status}"}

        now = datetime.now(timezone.utc)

        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return {"skipped": True, "reason": "agent has no strategy"}

        strategy = get_strategy(strategy_type)

        agent.last_tick_at = now
        agent.tick_count = (agent.tick_count or 0) + 1
        logger.info(
            "agent {} tick#{} status={} recovery={} session_trades={} total_trades={} pairs={}",
            agent.id, agent.tick_count, agent.status, agent.recovery_mode,
            agent.session_trade_count, agent.total_trades, agent.trading_pairs,
        )
        # Auto-normalize tiny accounts: relax max_risk_per_trade so the
        # RiskEngine can size a non-zero qty.  We no longer touch stop_loss_pct
        # here — SL comes from the strategy's own default_params merged with
        # agent.strategy_params, keeping each strategy's design intact.
        try:
            cap = float(agent.assigned_capital or 0)
            if cap > 0 and cap <= self._SMALL_ACCOUNT_CAP_THRESHOLD:
                applied = False
                # One-time cleanup: remove stale stop_loss_pct that was written
                # by older normalization code.  If the saved value exactly matches
                # the old 0.5% default AND the strategy's own SL is tighter,
                # it's a leftover override — remove it so the strategy default wins.
                sp = dict(agent.strategy_params or {})
                saved_sl = float(sp.get("stop_loss_pct") or 0)
                strat_default_sl = float((strategy.default_params or {}).get("stop_loss_pct", 0))
                if (
                    saved_sl == self._SMALL_ACCOUNT_DEFAULT_SL
                    and strat_default_sl > 0
                    and strat_default_sl < saved_sl
                ):
                    sp.pop("stop_loss_pct", None)
                    agent.strategy_params = sp
                    applied = True
                    logger.info(
                        "agent {} cleaned stale stop_loss_pct={:.2f}% from"
                        " strategy_params (strategy default={:.2f}%)",
                        agent.id, saved_sl, strat_default_sl,
                    )
                # Ensure max_risk_per_trade is at least the tiny-account default.
                # Note: RiskEngine.cap_risk_per_trade also allows 5% for capital <= $50.
                if (agent.max_risk_per_trade or 0) < self._SMALL_ACCOUNT_DEFAULT_MAX_RISK_PCT:
                    agent.max_risk_per_trade = float(self._SMALL_ACCOUNT_DEFAULT_MAX_RISK_PCT)
                    applied = True
                # One-time cleanup: if optimizer raised min_confidence above 0.60,
                # it will suppress all screener signals before the AI even sees them.
                # Reset to 0.50 so the AI still gets candidates to evaluate.
                saved_mc = float((sp.get("min_confidence") or 0))
                if saved_mc > 0.60:
                    sp["min_confidence"] = 0.50
                    agent.strategy_params = sp
                    applied = True
                    logger.info(
                        "agent {} cleaned over-high min_confidence={:.2f}→0.50",
                        agent.id, saved_mc,
                    )
                # Raise deviation_pct to 0.12% minimum — 0.06% is within 1m noise
                # floor (bid-ask + slippage) and was causing trades on random drift.
                # Any saved value below 0.10 gets raised to 0.12.
                saved_dev = float((sp.get("deviation_pct") or 0))
                if saved_dev < 0.10:
                    sp["deviation_pct"] = 0.12
                    agent.strategy_params = sp
                    applied = True
                    logger.info(
                        "agent {} raised deviation_pct {:.3f}→0.12 (below noise floor)",
                        agent.id, saved_dev,
                    )
                    logger.info(
                        "agent {} cleaned over-high deviation_pct={:.4f}→0.06",
                        agent.id, saved_dev,
                    )
                # One-time cleanup: SL ≤ 0.10% is within 1m bid-ask noise;
                # TP ≤ 0.30% barely covers Bitget fees after a winning trade.
                # Both stale values cause excessive stops and near-zero net wins.
                saved_sl = float((sp.get("stop_loss_pct") or 0))
                if 0 < saved_sl <= 0.10:
                    sp["stop_loss_pct"] = 0.20
                    agent.strategy_params = sp
                    applied = True
                    logger.info(
                        "agent {} cleaned noise-floor stop_loss_pct={:.3f}→0.20",
                        agent.id, saved_sl,
                    )
                saved_tp = float((sp.get("profit_target_pct") or 0))
                if 0 < saved_tp <= 0.30:
                    sp["profit_target_pct"] = 0.50
                    agent.strategy_params = sp
                    applied = True
                    logger.info(
                        "agent {} cleaned low profit_target_pct={:.3f}→0.50",
                        agent.id, saved_tp,
                    )
                if applied:
                    logger.info(
                        "agent {} small-account normalization: capital=${:.2f},"
                        " max_risk_per_trade={:.2f}%",
                        agent.id, cap, agent.max_risk_per_trade,
                    )
        except Exception as exc:
            logger.debug("agent {} small-account normalization error: {}", agent.id, exc)
        before_total_trades = int(agent.total_trades or 0)
        ai_used = False

        try:
            client = build_client(api_key)
        except PermissionError as exc:
            agent.last_error = str(exc)
            await agent.save()
            return {"skipped": True, "reason": str(exc)}

        # --- Auto-clean: remove crypto pairs from forex agents and vice versa ---
        if self._is_forex(api_key.exchange):
            clean_pairs = [p for p in (agent.trading_pairs or []) if not p.endswith("USDT")]
            if len(clean_pairs) != len(agent.trading_pairs or []):
                agent.trading_pairs = clean_pairs or ["EURUSD"]
                logger.info("agent {} cleaned crypto pairs from forex agent: {}", agent.id, agent.trading_pairs)

        # --- Daily profit protection + daily loss limit (both reset via rollover) ---
        capital = float(agent.assigned_capital or 0)
        if capital > 0:
            day_pnl = float(agent.current_day_pnl or 0)
            day_pnl_pct = (day_pnl / capital) * 100

            # Hard daily loss limit — pause until midnight rollover resets current_day_pnl.
            max_loss_pct = float(agent.max_daily_loss or 15)
            if day_pnl < 0 and abs(day_pnl_pct) >= max_loss_pct and agent.status == "active":
                agent.status = "paused"
                agent.last_error = f"daily loss limit {abs(day_pnl_pct):.1f}% >= {max_loss_pct:.0f}%"
                await agent.save()
                logger.warning(
                    "agent {} DAILY LOSS LIMIT: today {:.2f}% >= {:.1f}% — paused until rollover",
                    agent.id, abs(day_pnl_pct), max_loss_pct,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} hit daily loss limit ({abs(day_pnl_pct):.1f}% today)",
                    message=f"Trading paused to protect capital. Resets at midnight rollover. Max allowed: {max_loss_pct:.0f}%.",
                    data={"agent_id": str(agent.id), "trigger": "daily_loss_limit"},
                )
                return {"skipped": True, "reason": "daily_loss_limit"}

            protect_threshold = float(agent.profit_protect_pct or 15)
            if day_pnl_pct >= protect_threshold and not agent.protect_mode:
                agent.protect_mode = True
                logger.info(
                    "agent {} daily profit protection: today {:.1f}% >= {:.1f}% — protect mode ON",
                    agent.id, day_pnl_pct, protect_threshold,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} hit daily target ({day_pnl_pct:.1f}% today)",
                    message=f"Protecting today's gains. Higher confidence required for new entries. Resets tomorrow.",
                    data={"agent_id": str(agent.id), "trigger": "daily_profit_protect"},
                )

        # --- Fetch learning context once per tick so AI calls can use it ---
        learning_context: dict[str, Any] = {}
        try:
            from app.models.position import Position as PositionModel
            from app.models.trade import Trade as TradeModel

            # All currently open positions for this agent across ALL pairs.
            open_positions = await PositionModel.find(
                PositionModel.agent_id == agent.id,
                PositionModel.is_open == True,  # noqa: E712
            ).to_list()
            open_positions_summary = [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry_price": float(p.entry_price),
                    "current_price": float(p.current_price or p.entry_price),
                    "unrealized_pnl": float(p.unrealized_pnl or 0),
                    "stop_loss": float(p.stop_loss or 0),
                    "take_profit": float(p.take_profit or 0),
                }
                for p in open_positions
            ]
            open_symbols = {p.symbol for p in open_positions}

            recent = await TradeModel.find(
                TradeModel.agent_id == agent.id,
            ).sort(-TradeModel.created_at).limit(10).to_list()
            loss_streak = 0
            for t in recent:
                if t.status == "filled" and float(t.pnl or 0) < 0:
                    loss_streak += 1
                elif t.status == "filled":
                    break
            recent_summary = [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "status": t.status,
                    "pnl": float(t.pnl or 0),
                    "pnl_pct": float(t.pnl_pct or 0),
                    "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                    "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                }
                for t in recent
            ]
            learning_context = {
                "open_positions": open_positions_summary,
                "open_symbols": sorted(open_symbols),
                "open_position_count": len(open_positions),
                "recent_trades": recent_summary,
                "loss_streak": loss_streak,
                "total_pnl": float(agent.total_pnl or 0),
                "current_day_pnl": float(agent.current_day_pnl or 0),
                "winning_trades": int(agent.winning_trades or 0),
                "total_trades": int(agent.total_trades or 0),
                "win_rate_pct": round(
                    int(agent.winning_trades or 0) / max(int(agent.total_trades or 1), 1) * 100, 1
                ),
                "recovery_mode": bool(agent.recovery_mode),
                "max_concurrent_trades": int(agent.max_concurrent_trades or 1),
                # Guardian consciousness — shifts AI prompt tone
                "system_mood": getattr(agent, "system_mood", "neutral") or "neutral",
                "guardian_notes": getattr(agent, "guardian_notes", "") or "",
            }

            # Pre-fetch news sentiment for all trading pairs (one DB call per pair)
            try:
                from app.models.news_signal import NewsSignal
                from app.services.news_sentinel import symbol_to_coin

                market_sig = await NewsSignal.find_one(NewsSignal.coin == "MARKET")
                news_by_coin: dict[str, dict] = {}
                for _pair in list(agent.trading_pairs or []):
                    _coin = symbol_to_coin(_pair)
                    if _coin in news_by_coin:
                        continue
                    _sig = await NewsSignal.find_one(NewsSignal.coin == _coin)
                    _src = _sig or market_sig
                    if _src:
                        news_by_coin[_coin] = {
                            "coin": _coin,
                            "sentiment": _src.sentiment,
                            "score": float(_src.score),
                            "summary": _src.summary,
                            "key_events": list(_src.key_events or []),
                            "fear_greed_value": _src.fear_greed_value,
                            "fear_greed_label": _src.fear_greed_label,
                            "updated_at": _src.updated_at.isoformat() if _src.updated_at else None,
                        }
                if news_by_coin:
                    learning_context["news_by_coin"] = news_by_coin
                if market_sig:
                    learning_context["market_fear_greed"] = {
                        "value": market_sig.fear_greed_value,
                        "label": market_sig.fear_greed_label,
                    }
            except Exception as _news_exc:
                logger.debug("agent {} news context fetch failed: {}", agent.id, _news_exc)

        except Exception as exc:
            logger.debug("agent {} learning context fetch failed: {}", agent.id, exc)

        # --- Two-phase scan: screener filters, AI analyses top candidates ---
        # Phase 1: Run screener on ALL symbols (free, no API calls)
        candidates: list[tuple[str, Any, Any, Any]] = []  # (symbol, df, screener_signal, open_pos)
        signals_summary: list[str] = []

        # Multi-timeframe: map 1m → 5m, 5m → 15m, 15m → 1h, else no HTF
        _HTF_MAP = {"1m": "5m", "3m": "15m", "5m": "15m", "15m": "1h", "30m": "4h"}
        htf = _HTF_MAP.get(agent.timeframe)

        try:
            for symbol in list(agent.trading_pairs or []):
                try:
                    raw = await client.get_candles(symbol, agent.timeframe, limit=200)
                except ExchangeError as exc:
                    if self._is_symbol_removed_error(exc):
                        agent.trading_pairs = [p for p in (agent.trading_pairs or []) if p != symbol]
                        await agent.save()
                        logger.warning(
                            "agent {} {}: symbol removed from exchange, dropping from trading pairs",
                            agent.id,
                            symbol,
                        )
                        continue
                    if symbol not in self._KNOWN_UNAVAILABLE:
                        logger.warning("agent {} {}: candle fetch failed: {}", agent.id, symbol, exc)
                    continue
                if len(raw) < 50:
                    continue

                df = candles_to_df(raw)
                open_position = await self._open_position(agent.id, symbol)
                last_price = float(df["close"].iloc[-1])

                # TP/SL auto-close — for paper trades uses candle high/low for
                # realistic fill detection; adds slippage to simulate exchange behaviour.
                if open_position is not None:
                    open_position.current_price = last_price
                    sl = float(open_position.stop_loss or 0)
                    tp = float(open_position.take_profit or 0)
                    if agent.is_paper_trade:
                        import random as _random
                        candle_high = float(df["high"].iloc[-1])
                        candle_low = float(df["low"].iloc[-1])
                        if open_position.side == "long":
                            hit_sl = sl > 0 and candle_low <= sl
                            hit_tp = tp > 0 and candle_high >= tp
                        else:
                            hit_sl = sl > 0 and candle_high >= sl
                            hit_tp = tp > 0 and candle_low <= tp
                        if hit_tp:
                            # Simulate partial slippage: TP fills slightly under the level
                            slip = _random.uniform(0.0001, 0.0004)
                            fill = tp * (1 - slip) if open_position.side == "long" else tp * (1 + slip)
                            await self._close_position(agent, api_key, client, open_position, fill, "paper_tp")
                            signals_summary.append(f"{symbol}:TP")
                            continue
                        if hit_sl:
                            # Simulate gap-through slippage: SL fills slightly past the level
                            slip = _random.uniform(0.0002, 0.0010)
                            fill = sl * (1 - slip) if open_position.side == "long" else sl * (1 + slip)
                            await self._close_position(agent, api_key, client, open_position, fill, "paper_sl")
                            signals_summary.append(f"{symbol}:SL")
                    else:
                        # Live trade: just check close price; exchange handles actual SL/TP fills
                        if open_position.side == "long":
                            hit_sl = sl > 0 and last_price <= sl
                            hit_tp = tp > 0 and last_price >= tp
                        else:
                            hit_sl = sl > 0 and last_price >= sl
                            hit_tp = tp > 0 and last_price <= tp
                        if hit_tp:
                            await self._close_position(agent, api_key, client, open_position, tp, "take profit hit")
                            signals_summary.append(f"{symbol}:TP")
                            continue
                        if hit_sl:
                            await self._close_position(agent, api_key, client, open_position, sl, "stop loss hit")
                            signals_summary.append(f"{symbol}:SL")
                        continue

                ctx = StrategyContext(
                    symbol=symbol,
                    timeframe=agent.timeframe,
                    in_position=open_position is not None,
                    position_side=open_position.side if open_position else None,
                )
                screener_signal = strategy.evaluate(df, agent.strategy_params or {}, ctx)

                # Multi-timeframe filter: skip new entries that fight the HTF trend.
                # Only applies to entry signals — exits and holds are never blocked.
                if htf and screener_signal.action in ("enter_long", "enter_short") and open_position is None:
                    try:
                        raw_htf = await client.get_candles(symbol, htf, limit=50)
                        if len(raw_htf) >= 20:
                            df_htf = candles_to_df(raw_htf)
                            from app.services.strategy.indicators import ema as _ema
                            htf_close = df_htf["close"]
                            htf_ema20 = _ema(htf_close, 20)
                            htf_slope = (
                                (float(htf_ema20.iloc[-1]) - float(htf_ema20.iloc[-10]))
                                / float(htf_ema20.iloc[-10]) * 100
                            )
                            # Strong HTF trend against the signal = skip
                            _HTF_SLOPE_BLOCK = 0.08  # % over 10 bars
                            htf_blocks_long = screener_signal.action == "enter_long" and htf_slope < -_HTF_SLOPE_BLOCK
                            htf_blocks_short = screener_signal.action == "enter_short" and htf_slope > _HTF_SLOPE_BLOCK
                            if htf_blocks_long or htf_blocks_short:
                                logger.debug(
                                    "agent {} {} {} blocked by HTF {} trend (slope={:.3f}%)",
                                    agent.id, symbol, screener_signal.action, htf, htf_slope,
                                )
                                continue
                            # Attach HTF context to signal metadata for GPT
                            if screener_signal.metadata is None:
                                screener_signal.metadata = {}
                            screener_signal.metadata["htf_timeframe"] = htf
                            screener_signal.metadata["htf_slope"] = round(htf_slope, 4)
                    except Exception:
                        pass  # HTF fetch failed — don't block the signal, just skip the filter

                # Always include: non-hold signals, open positions
                # Conditionally include: holds with high confidence (near threshold)
                if (
                    screener_signal.action != "hold"
                    or open_position is not None
                    or screener_signal.confidence > 0.35
                ):
                    candidates.append((symbol, df, screener_signal, open_position))

            candidates.sort(key=lambda c: (c[2].action == "hold", -c[2].confidence))

            # Phase 2: GPT makes the final approve/reject on the screener's best pick.
            # Groq is removed — screener signals go directly to GPT.
            # GPT only gets called when the screener has an actionable signal
            # (enter_long / enter_short / exit). Holds skip GPT entirely.
            gpt_best = None  # (symbol, df, screener_signal, open_pos)
            decision_entry: dict | None = None  # captured for ai_decision_log
            _exit_handled = False  # prevents double-exit when candidates loop already ran one

            for symbol, df, screener_signal, open_position in candidates[:1]:
                logger.info(
                    "agent {} screener best: {} {} conf={:.2f} regime={} reversal_pending={}",
                    agent.id, symbol, screener_signal.action,
                    screener_signal.confidence,
                    (screener_signal.metadata or {}).get("market_regime", "?"),
                    (screener_signal.metadata or {}).get("reversal_pending", False),
                )
                decision_entry = {
                    "ts": now.isoformat(),
                    "symbol": symbol,
                    "screener": {
                        "action": screener_signal.action,
                        "confidence": round(screener_signal.confidence, 3),
                        "reason": screener_signal.reason,
                        "regime": (screener_signal.metadata or {}).get("market_regime", "?"),
                        "reversal_pending": bool((screener_signal.metadata or {}).get("reversal_pending", False)),
                    },
                    "gpt": None,
                    "final": "screener_hold",
                    "trade_placed": False,
                }
                # Exits skip GPT — they are time-critical and must not wait on a
                # rate-limited API call. The screener's exit logic is deterministic
                # (price reverted to EMA, or SL/TP hit) so GPT adds no value here.
                if screener_signal.action in ("enter_long", "enter_short"):
                    gpt_best = (symbol, df, screener_signal, open_position)
                elif screener_signal.action == "exit" and open_position is not None:
                    gpt_best = None
                    _exit_handled = True
                    agent.last_signal = "exit"
                    agent.last_signal_symbol = symbol
                    if decision_entry:
                        decision_entry["final"] = "exit"
                        decision_entry["gpt"] = {"decision": "screener_exit", "confidence": screener_signal.confidence, "reason": "exit executed directly — no GPT needed"}
                    await self._execute_signal(
                        agent, api_key, client, symbol, df,
                        screener_signal, open_position, ai_available=True,
                    )
                    signals_summary.append(f"{symbol}:exit")

            # GPT final decision — only runs when screener has an actionable signal
            if gpt_best:
                symbol, df, screener_signal, open_position = gpt_best
                try:
                    ai_used = True
                    final_signal = await grok_analyst.gpt_decide(
                        screener_signal, df,
                        symbol=symbol, timeframe=agent.timeframe,
                        agent_name=agent.name,
                        learning_context=learning_context,
                    )
                    gpt_verdict = (final_signal.metadata or {}).get("gpt_decision", "approve")
                    if decision_entry:
                        decision_entry["gpt"] = {
                            "decision": gpt_verdict,
                            "confidence": round(final_signal.confidence, 3),
                            "reason": final_signal.reason,
                        }
                        decision_entry["final"] = final_signal.action
                    agent.last_signal = final_signal.action
                    agent.last_signal_symbol = symbol
                    await self._execute_signal(
                        agent, api_key, client, symbol, df,
                        final_signal, open_position, ai_available=True,
                    )
                    if final_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{final_signal.action}")
                        if decision_entry:
                            decision_entry["trade_placed"] = True
                except Exception as exc:
                    # GPT failed (rate-limit, timeout, etc.).
                    # Entries require GPT approval — block them when GPT is unavailable.
                    # Exits are capital-critical and still execute from the screener.
                    is_entry = screener_signal.action in ("enter_long", "enter_short")
                    logger.warning(
                        "agent {} GPT decision failed for {} ({}): {} — {}",
                        agent.id, symbol, screener_signal.action, exc,
                        "BLOCKING entry (no unreviewed entries)" if is_entry else "executing exit directly",
                    )
                    if decision_entry:
                        decision_entry["gpt"] = {
                            "decision": "blocked" if is_entry else "gpt_unavailable_exit",
                            "confidence": 0,
                            "reason": f"GPT unavailable: {exc}",
                        }
                        decision_entry["final"] = "hold" if is_entry else screener_signal.action
                    if is_entry:
                        # Do not enter without GPT approval — too many false signals at this confidence level
                        agent.last_signal = "hold"
                        agent.last_signal_symbol = f"{symbol} (GPT unavailable)"
                    else:
                        agent.last_signal = screener_signal.action
                        agent.last_signal_symbol = symbol
                        await self._execute_signal(
                            agent, api_key, client, symbol, df,
                            screener_signal, open_position, ai_available=False,
                        )
                        if screener_signal.action != "hold":
                            signals_summary.append(f"{symbol}:{screener_signal.action}")
                            if decision_entry:
                                decision_entry["trade_placed"] = True
            elif candidates and not _exit_handled:
                best = candidates[0]
                symbol, df, screener_signal, open_position = best
                if screener_signal.action == "exit" and open_position is not None:
                    # Screener wants to exit an open position — allow it even if GPT
                    # is offline, to protect capital.
                    logger.info("agent {} screener exit signal for {} (conf {:.2f}) — executing without GPT",
                                agent.id, symbol, screener_signal.confidence)
                    agent.last_signal = screener_signal.action
                    agent.last_signal_symbol = f"{symbol} (screener exit)"
                    try:
                        await self._execute_signal(
                            agent, api_key, client, symbol, df,
                            screener_signal, open_position, ai_available=False,
                        )
                    except Exception as exc:
                        logger.error("agent {} _execute_signal for {} raised exception: {}", agent.id, symbol, exc, exc_info=True)
                    if screener_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{screener_signal.action}")
                else:
                    # Screener returned hold on best candidate — nothing to do.
                    logger.debug(
                        "agent {} screener hold for {} (conf {:.2f}) — no entry this tick",
                        agent.id, symbol, screener_signal.confidence,
                    )

        finally:
            # Reconcile before closing the client so get_positions still works.
            try:
                await self._reconcile_open_positions(agent, api_key, client)
            except Exception as exc:
                logger.debug("agent {} reconciliation failed: {}", agent.id, exc)
            await client.close()

        pairs_checked = len(agent.trading_pairs or [])
        if signals_summary:
            agent.last_error = None
            agent.last_signal_symbol = " | ".join(signals_summary[:3])
        else:
            agent.last_signal = "hold"
            agent.last_signal_symbol = f"scanned {pairs_checked} pairs"
            agent.last_error = None

        # Append AI decision entry to the live log (newest first, keep last 20).
        if decision_entry:
            log = list(agent.ai_decision_log or [])
            log.insert(0, decision_entry)
            agent.ai_decision_log = log[:20]

        # Stuck agent nudge: every 200 ticks (~3h at 60s interval) if no trades
        tick_count = agent.tick_count or 0
        if tick_count > 0 and tick_count % 200 == 0 and agent.session_trade_count == 0:
            try:
                nudge = await grok_analyst.nudge_stuck_agent(
                    agent_data={
                        "name": agent.name,
                        "strategy": agent.strategy.type if agent.strategy else None,
                        "strategy_params": agent.strategy_params or {},
                        "timeframe": agent.timeframe,
                        "trading_pairs": agent.trading_pairs,
                        "total_trades": agent.total_trades,
                        "tick_count": tick_count,
                    },
                    last_signals=["hold"] * 20,
                )
                if nudge and nudge.get("suggested_params"):
                    agent.strategy_params = {**(agent.strategy_params or {}), **nudge["suggested_params"]}
                    agent.last_error = f"AI nudge: {nudge.get('diagnosis', 'adjusting params')}"
                    logger.info("agent {} AI nudge: {}", agent.id, nudge.get("diagnosis"))
            except Exception:
                pass

        await agent.save()
        trades_opened = int(agent.total_trades or 0) - before_total_trades
        candidates_checked = len(agent.trading_pairs or [])
        signals_emitted = len(signals_summary)
        logger.info(
            "agent_metric {} candidates={} signals={} trades_opened={} ai_used={}",
            agent.id,
            candidates_checked,
            signals_emitted,
            trades_opened,
            ai_used,
        )
        return {"ok": True}

    # Bybit SELL order minimum quantities (per symbol base currency)
    # These are the actual minimums Bybit enforces, NOT the notional minimums.
    _MIN_QTY: dict[str, float] = {
        "BTC": 0.001,
        "ETH": 0.03,      # Bybit SELL minimum for ETHUSDT
        "SOL": 0.1,
        "BNB": 0.01,
        "XRP": 1.0,
    }
    _DEFAULT_MIN_QTY = 0.03

    # Forex pairs have different minimums (in units of base currency)
    _FOREX_MIN_QTY = 1  # OANDA allows 1 unit minimum

    @staticmethod
    def _is_forex(exchange: str | None) -> bool:
        if not exchange:
            return False
        return str(exchange).strip().lower() in {"oanda", "mt5", "forex", "fx"}

    @classmethod
    def _min_qty_for(cls, symbol: str, exchange: str = "", side: str | None = None) -> float:
        if cls._is_forex(exchange):
            return cls._FOREX_MIN_QTY
        exchange_name = (exchange or "").lower()
        if exchange_name == "bitget":
            return 0.01
        for prefix, min_q in cls._MIN_QTY.items():
            if symbol.upper().startswith(prefix):
                # Some exchanges enforce different minimums for sell (short) vs buy
                # on certain instruments (empirically observed on Bybit ETHUSDT).
                if prefix == "ETH" and side and side.lower() == "short":
                    # observed sell minimum around 0.03 on Bybit for ETH
                    return max(min_q, 0.03)
                return min_q
        return cls._DEFAULT_MIN_QTY

    @staticmethod
    def _coerce_step_and_minimum(payload: dict[str, Any]) -> tuple[float, float]:
        def _first_numeric(*keys: str) -> float | None:
            for key in keys:
                value = payload.get(key)
                if value in (None, ""):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return None

        step = _first_numeric("sizeIncrement", "qtyStep", "step", "minTradeNum")
        min_order = _first_numeric("minTradeNum", "minTradeAmount", "minOrderSize", "minSize", "minQty", "minOrderQty")
        # Try to extract price tick / price increment for rounding SL/TP.
        # Bitget exposes this as `pricePlace` (decimal places) rather than a numeric tick.
        price_tick = _first_numeric(
            "priceTick",
            "priceIncrement",
            "tickSize",
            "priceStep",
            "minPrice",
            "tick",
        )
        if price_tick is None:
            price_place = _first_numeric("pricePlace")
            if price_place is not None:
                price_tick = 10 ** (-int(price_place))
        if step is None:
            step = 1.0
        if min_order is None:
            min_order = step
        # If price_tick not found, default to None (caller may ignore)
        return step, min_order, price_tick

    async def _adjust_quantity_for_exchange(self, client, symbol: str, qty: float) -> tuple[float, float, float | None]:
        """Query the exchange for lot/step info and return (adjusted_qty, exchange_min_qty, price_tick).

        adjusted_qty is qty rounded DOWN to the nearest allowed `qtyStep`.
        exchange_min_qty is the instrument minimum if available, else the
        engine default from `_min_qty_for`.
        """
        exchange_name = (getattr(client, "name", "") or "").lower()

        if exchange_name == "bitget":
            try:
                res = await client._public("/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"})
                items = res if isinstance(res, list) else []
                contract = next(
                    (item for item in items if str(item.get("symbol", "")).upper() == symbol.upper()),
                    None,
                )
                if contract is None and items:
                    contract = items[0]
                if contract:
                    step, min_order, price_tick = self._coerce_step_and_minimum(contract)
                    if step > 0:
                        from decimal import Decimal, ROUND_DOWN

                        dec_qty = Decimal(str(qty))
                        dec_step = Decimal(str(step))
                        steps = (dec_qty / dec_step).to_integral_value(rounding=ROUND_DOWN)
                        adj = float(steps * dec_step)
                        if adj <= 0 and min_order > 0:
                            adj = float(min_order)
                        exchange_min = max(float(min_order), self._min_qty_for(symbol, exchange=exchange_name))
                        return adj, exchange_min, price_tick
            except Exception:
                pass

            fallback_min = self._min_qty_for(symbol, exchange=exchange_name)
            return max(qty, fallback_min), fallback_min, None

        try:
            res = await client._signed(
                "GET",
                "/v5/market/instruments-info",
                params={"category": "linear", "symbol": symbol},
            )
            items = res.get("list") or []
            if items:
                inst = items[0]
                lot = inst.get("lotSizeFilter") or {}
                step = float(lot.get("qtyStep") or 0) or 0.0
                min_order = float(lot.get("minOrderQty") or 0) or 0.0
                pf = inst.get("priceFilter") or {}
                price_tick = None
                try:
                    price_tick = float(
                        pf.get("tickSize") or pf.get("priceTick") or pf.get("priceIncrement") or 0
                    ) or None
                except Exception:
                    price_tick = None

                if step > 0:
                    from decimal import Decimal, ROUND_DOWN

                    dec_qty = Decimal(str(qty))
                    dec_step = Decimal(str(step))
                    steps = (dec_qty / dec_step).to_integral_value(rounding=ROUND_DOWN)
                    adj = float(steps * dec_step)
                    if adj <= 0 and min_order > 0:
                        adj = float(min_order)
                    exchange_min = min_order if min_order > 0 else self._min_qty_for(symbol)
                    return adj, exchange_min, price_tick
        except Exception:
            pass

        fallback_min = self._min_qty_for(symbol)
        return max(qty, fallback_min), fallback_min, None

    @staticmethod
    def _is_symbol_removed_error(exc: Exception) -> bool:
        return isinstance(exc, ExchangeError) and "The symbol has been removed" in str(exc)

    # Symbols that persistently fail on all fallback providers — downgraded to
    # debug so they don't pollute logs on every tick.
    _KNOWN_UNAVAILABLE: frozenset[str] = frozenset({"TONUSDT"})

    async def _open_position(self, agent_id: uuid.UUID, symbol: str) -> Position | None:
        """Fetch the current open position for an agent and symbol."""
        positions = await Position.find(
            Position.agent_id == agent_id,
            Position.symbol == symbol,
            Position.is_open == True,  # noqa: E712
        ).sort(-Position.opened_at).to_list()
        return positions[0] if positions else None

    async def _reconcile_open_positions(self, agent: Agent, api_key, client) -> None:
        """Update unrealized P&L for open positions every tick.

        Close detection is intentionally NOT done here — it belongs to:
          • position_stream.py  — real-time via Bitget private WebSocket (primary)
          • scheduler_jobs.py   — 5-min fallback for WS-missed fills

        Doing close detection in every 60s tick via get_positions() was causing
        false-closes: a market order placed seconds earlier wasn't reflected in
        Bitget's position API yet, so the position appeared missing and was
        immediately closed in the DB while still open on the exchange.
        """
        positions = await Position.find(
            Position.agent_id == agent.id,
            Position.is_open == True,  # noqa: E712
        ).to_list()
        if not positions:
            return

        # ── Update unrealized P&L for still-open positions ────────────────
            try:
                raw = await client.get_candles(pos.symbol, agent.timeframe or "5m", limit=2)
                if raw:
                    from app.services.strategy.indicators import candles_to_df
                    df = candles_to_df(raw)
                    current = float(df["close"].iloc[-1])
                else:
                    current = float(pos.current_price or pos.entry_price)
            except Exception:
                current = float(pos.current_price or pos.entry_price)

            try:
                entry = float(pos.entry_price)
                qty = float(pos.quantity)
                raw = (current - entry) * qty
                if pos.side == "short":
                    raw = -raw
                # Include round-trip fee so unrealized P&L reflects true balance impact
                gross = raw - (entry * qty * 0.00120)

                pos.current_price = current
                pos.unrealized_pnl = gross
                pos.unrealized_pnl_pct = (gross / max(entry * qty, 1e-9)) * 100
                pos.updated_at = datetime.now(timezone.utc)
                await pos.save()

                if pos.trade_id:
                    try:
                        trade = await Trade.find_one(Trade.id == pos.trade_id)
                        if trade and trade.status == "open":
                            trade.notes = f"unrealized_pnl={pos.unrealized_pnl:.2f}"
                            await trade.save()
                    except Exception:
                        pass
            except Exception:
                logger.debug("agent {} failed to reconcile position {}", agent.id, pos.id)

    async def _close_position_from_exchange(
        self,
        agent: Agent,
        api_key,
        pos: "Position",
        closed_orders_by_symbol: dict[str, list[dict]],
    ) -> None:
        """Close a DB position that the exchange already closed server-side.

        Resolves the actual exit fill price from order history then mirrors
        exactly what _close_position does for paper trades: persists PnL,
        updates agent counters, and feeds the learning system.
        """
        entry_price = float(pos.entry_price)
        qty = float(pos.quantity or 0)

        # Match fill: try exact order_id first, then symbol+qty proximity.
        sym_orders = closed_orders_by_symbol.get(str(pos.symbol).upper(), [])
        actual_fill: dict | None = None
        linked_oid: str = ""
        if pos.trade_id:
            try:
                linked_trade = await Trade.find_one(Trade.id == pos.trade_id)
                linked_oid = str(linked_trade.exchange_order_id or "") if linked_trade else ""
            except Exception:
                pass
        if linked_oid:
            actual_fill = next((o for o in sym_orders if o["order_id"] == linked_oid), None)
        if actual_fill is None and sym_orders:
            qty_matches = [
                o for o in sym_orders
                if abs(o["filled_qty"] - qty) / max(qty, 1e-9) < 0.05
            ]
            if qty_matches:
                # Prefer closing orders (tradeSide=="close") over opening orders
                # so we match the SL/TP fill, not the entry.
                close_orders = [o for o in qty_matches if o.get("trade_side") == "close"]
                pool = close_orders if close_orders else qty_matches
                actual_fill = max(pool, key=lambda o: o["closed_at_ms"])

        # Bitget taker fee: 0.06% per side = 0.12% round-trip on notional.
        _fee_rate = 0.00120

        if actual_fill and actual_fill["avg_fill_price"] > 0:
            exit_price = actual_fill["avg_fill_price"]
            if actual_fill["pnl"] != 0:
                # Use exchange-reported PnL — already net of fees.
                gross = actual_fill["pnl"]
            else:
                # Exchange PnL missing; estimate from prices and deduct fees.
                raw_pnl = (exit_price - entry_price) * qty * (1 if pos.side == "long" else -1)
                fees = entry_price * qty * _fee_rate
                gross = raw_pnl - fees
            price_source = "exchange_fill"
        else:
            exit_price = float(pos.current_price or entry_price)
            raw_pnl = (exit_price - entry_price) * qty
            if pos.side == "short":
                raw_pnl = -raw_pnl
            fees = entry_price * qty * _fee_rate
            gross = raw_pnl - fees
            price_source = "candle_estimate"

        # Persist the closure.
        pos.is_open = False
        pos.current_price = exit_price
        pos.unrealized_pnl = gross
        pos.updated_at = datetime.now(timezone.utc)
        await pos.save()

        if pos.trade_id:
            try:
                trade = await Trade.find_one(Trade.id == pos.trade_id)
                if trade and trade.status == "open":
                    trade.exit_price = exit_price
                    trade.pnl = gross
                    trade.pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
                    trade.status = "filled"
                    trade.closed_at = datetime.now(timezone.utc)
                    trade.notes = f"closed by exchange (price_source={price_source})"
                    await trade.save()
            except Exception:
                pass

        # Update agent counters — same as _close_position.
        agent.total_pnl = float(agent.total_pnl or 0) + gross
        agent.current_day_pnl = float(agent.current_day_pnl or 0) + gross
        agent.current_week_pnl = float(agent.current_week_pnl or 0) + gross
        if gross > 0:
            agent.winning_trades = (agent.winning_trades or 0) + 1
            agent.recovery_mode = False  # winning trade clears recovery mode
            agent.pause_cycle_count = 0  # reset pause cycle count on any win
        await agent.save()

        # Feed the learning system.
        try:
            from app.services.learning import LearningService
            strategy_type = agent.strategy.type if agent.strategy else None
            if strategy_type:
                await LearningService.record_trade_outcome(
                    agent_id=agent.id,
                    strategy_type=strategy_type,
                    symbol=pos.symbol,
                    timeframe=agent.timeframe,
                    pnl=gross,
                )
        except Exception:
            pass

        await NotificationService.create(
            user_id=agent.user_id,
            type="trade_closed",
            title=f"{agent.name} closed {pos.side} {pos.symbol}",
            message=f"PnL {gross:+.4f} (closed by exchange, {price_source})",
            data={"agent_id": str(agent.id), "trade_id": str(pos.trade_id)},
        )

        # Check loss streak and trigger recovery/pause — same as _close_position.
        if gross < 0:
            await self._check_loss_streak_and_recover(agent, pos.symbol)

        logger.info(
            "agent {} {} position closed by exchange: side={} pnl={:.4f} exit={} source={}",
            agent.id, pos.symbol, pos.side, gross, exit_price, price_source,
        )

    async def _tick_symbol_ai(self, agent: Agent, api_key, strategy, client, symbol: str, df, screener_signal, open_position) -> None:
        """Process a symbol with AI analysis — called for top candidates only."""
        last_price = float(df["close"].iloc[-1])
        win_rate = (agent.winning_trades / agent.total_trades) if agent.total_trades > 0 else 0.5

        ai_available = True
        try:
            signal = await grok_analyst.analyse_market(
                screener_signal,
                df,
                symbol=symbol,
                timeframe=agent.timeframe,
                strategy_type=strategy.type,
                strategy_params=agent.strategy_params or {},
                agent_context={
                    "agent_id": str(agent.id),
                    "agent_name": agent.name,
                    "total_trades": agent.total_trades,
                    "winning_trades": agent.winning_trades,
                    "win_rate": f"{win_rate:.0%}",
                    "total_pnl": float(agent.total_pnl or 0),
                    "current_day_pnl": float(agent.current_day_pnl or 0),
                    "in_position": open_position is not None,
                    "is_protect_mode": getattr(agent, "protect_mode", False),
                    "screener_said": screener_signal.action,
                    "screener_confidence": screener_signal.confidence,
                    "screener_reason": screener_signal.reason,
                },
            )
        except Exception as exc:
            logger.warning("agent {} {} AI failed: {}", agent.id, symbol, exc)
            signal = screener_signal
            ai_available = False

        agent.last_signal = signal.action
        agent.last_signal_symbol = symbol
        agent.last_error = None

        logger.debug(
            "agent {} {} signal={} price={:.4f} confidence={:.2f}",
            agent.id, symbol, signal.action, last_price, signal.confidence,
        )

        await self._execute_signal(
            agent,
            api_key,
            client,
            symbol,
            df,
            signal,
            open_position,
            ai_available=ai_available,
        )

    async def _execute_signal(
        self,
        agent,
        api_key,
        client,
        symbol,
        df,
        signal,
        open_position,
        *,
        ai_available: bool = True,
    ) -> None:
        """Execute a trading signal (entry, exit, or hold)."""
        last_price = float(df["close"].iloc[-1])
        logger.debug("agent {} _execute_signal {} action={} conf={:.2f} last_price={}", 
                     agent.id, symbol, signal.action, signal.confidence, last_price)

        if signal.action == "hold":
            logger.debug("agent {} _execute_signal {} skipping hold", agent.id, symbol)
            return

        if signal.action == "exit" and open_position is not None:
            logger.debug("agent {} _execute_signal {} closing position", agent.id, symbol)
            await self._close_position(agent, api_key, client, open_position, last_price, signal.reason)
            return

        if signal.action in ("enter_long", "enter_short") and open_position is None:
            min_conf = get_entry_confidence_threshold(
                protect_mode=bool(agent.protect_mode),
                ai_available=ai_available,
            )
            if not should_execute_entry_signal(
                signal.confidence,
                protect_mode=bool(agent.protect_mode),
                ai_available=ai_available,
            ):
                agent.last_error = f"AI confidence {signal.confidence:.2f} < {min_conf}"
                logger.info("agent {} {} REJECTED: confidence {:.2f} < {:.2f} (ai_available={})", agent.id, symbol, signal.confidence, min_conf, ai_available)
                return

            side: str = "long" if signal.action == "enter_long" else "short"

            # Merge strategy design defaults with any agent-level overrides.
            # Strategy default_params (e.g. 0.10% SL for micro_scalping) are
            # the base; agent.strategy_params can tighten/widen from there.
            _strategy_type = agent.strategy.type if agent.strategy else None
            _strat_obj = get_strategy(_strategy_type) if _strategy_type else None
            strat_params = (
                _strat_obj.merge_params(agent.strategy_params)
                if _strat_obj else (agent.strategy_params or {})
            )
            default_sl = float(strat_params.get("stop_loss_pct", 1.0))
            default_tp = float(strat_params.get("take_profit_pct",
                              strat_params.get("profit_target_pct", 2.5)))

            ai_sl = signal.suggested_stop_loss_pct or default_sl
            ai_tp = signal.suggested_take_profit_pct or default_tp

            # SL: use the TIGHTER of strategy default and AI suggestion
            sl_pct = min(ai_sl, default_sl)
            # TP: use the LARGER of strategy default and AI suggestion
            tp_pct = max(ai_tp, default_tp)

            # Hard floor: TP must clear round-trip fees (Bitget taker 0.06% × 2)
            # plus a small margin of profit.  Without this a near-zero TP will
            # be hit immediately with guaranteed loss after fees.
            _MIN_TP_PCT = 0.25
            tp_pct = max(tp_pct, _MIN_TP_PCT)

            # Also ensure TP > SL (no inverted risk/reward).
            if tp_pct < sl_pct:
                tp_pct = round(sl_pct * 1.5, 3)

            logger.info(
                "agent {} {} SL/TP: sl={:.3f}% tp={:.3f}% (strategy defaults sl={:.3f}% tp={:.3f}%)",
                agent.id, symbol, sl_pct, tp_pct, default_sl, default_tp,
            )

            decision = await RiskEngine.evaluate_entry(
                agent,
                entry_price=last_price,
                stop_loss_pct=sl_pct,
                side=side,  # type: ignore[arg-type]
            )
            logger.debug("agent {} {} RiskEngine decision: approved={} qty={} reason={}", 
                         agent.id, symbol, decision.approved, decision.sized_quantity, decision.reason)
            if not decision.approved:
                agent.last_error = f"risk blocked: {decision.reason}"
                logger.info("agent {} {} RISK BLOCKED: {}", agent.id, symbol, decision.reason)
                return

            # Enforce exchange minimum order size and step — prevent wasted API calls.
            qty = decision.sized_quantity
            # Ask the exchange for instrument step/min info and adjust qty accordingly.
            logger.debug("agent {} {} asking exchange for lot/step info for qty={}", agent.id, symbol, qty)
            adj_qty, exchange_min, price_tick = await self._adjust_quantity_for_exchange(client, symbol, qty)
            logger.debug("agent {} {} exchange response: adj_qty={} exchange_min={} price_tick={}", 
                         agent.id, symbol, adj_qty, exchange_min, price_tick)
            if adj_qty < exchange_min:
                msg = (
                    f"position size {qty:.6f} adjusted to {adj_qty:.6f} below exchange minimum {exchange_min} for {symbol}. "
                    f"Increase assigned capital (current: ${agent.assigned_capital:.0f}) or tighten SL."
                )
                agent.last_error = msg
                logger.warning("agent {} {}: {}", agent.id, symbol, msg)
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="min_qty",
                    severity="warning",
                    message=msg,
                    details={"symbol": symbol, "qty": qty, "adjusted_qty": adj_qty, "min_qty": exchange_min},
                )
                return
            # Use adjusted qty for placement
            qty = adj_qty

            # Compute explicit SL/TP prices and quantize them to the instrument price tick when available
            from decimal import Decimal, ROUND_DOWN, ROUND_UP

            def _quantize_price(val: float, tick: float | None, rounding):
                if tick is None:
                    return float(round(val, 8))
                dec = Decimal(str(val))
                quantum = Decimal(str(tick))
                return float(dec.quantize(quantum, rounding=rounding))

            # raw prices
            raw_sl = (last_price * (1 - sl_pct / 100)) if side == "long" else (last_price * (1 + sl_pct / 100))
            raw_tp = (last_price * (1 + tp_pct / 100)) if side == "long" else (last_price * (1 - tp_pct / 100))

            # For longs: SL (below) -> ROUND_DOWN, TP (above) -> ROUND_UP
            # For shorts: SL (above) -> ROUND_UP, TP (below) -> ROUND_DOWN
            if side == "long":
                sl_price = _quantize_price(raw_sl, price_tick, ROUND_DOWN)
                tp_price = _quantize_price(raw_tp, price_tick, ROUND_UP)
            else:
                sl_price = _quantize_price(raw_sl, price_tick, ROUND_UP)
                tp_price = _quantize_price(raw_tp, price_tick, ROUND_DOWN)

            logger.info("agent {} {} PLACING ORDER: symbol={} side={} qty={} entry={} SL={} TP={}", 
                        agent.id, symbol, symbol, side, qty, last_price, sl_price, tp_price)

            # Place the order through the exchange client; pass explicit rounded SL/TP
            await self._open_trade(
                agent=agent,
                api_key=api_key,
                client=client,
                symbol=symbol,
                side=side,
                entry_price=last_price,
                quantity=qty,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                signal=signal,
                risk_payload={
                    "approved": True,
                    "reason": decision.reason,
                    "risk_amount": decision.risk_amount,
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                },
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                min_qty=exchange_min,
                decision_context={
                    "ai_available": ai_available,
                    "strategy_params_snapshot": strat_params,
                    "sl_source": "strategy_default" if ai_sl >= default_sl else "ai_tightened",
                    "tp_source": "min_floor" if tp_pct == _MIN_TP_PCT else ("ai_suggested" if ai_tp > default_tp else "strategy_default"),
                    "recovery_mode": bool(agent.recovery_mode),
                },
            )
            logger.debug("agent {} {} _open_trade completed", agent.id, symbol)

    async def _persist_open_trade(
        self,
        *,
        agent: Agent,
        api_key,
        placed: OrderResult,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal,
        risk_payload: dict[str, Any],
        decision_context: dict[str, Any] | None = None,
    ) -> None:
        sl_price = (
            entry_price * (1 - stop_loss_pct / 100)
            if side == "long"
            else entry_price * (1 + stop_loss_pct / 100)
        )
        tp_price = (
            entry_price * (1 + take_profit_pct / 100)
            if side == "long"
            else entry_price * (1 - take_profit_pct / 100)
        )

        logger.info("agent {} {} persisting trade doc for symbol={} qty={}", agent.id, symbol, symbol, quantity)
        trade = Trade(
            user_id=agent.user_id,
            agent_id=agent.id,
            api_key_id=api_key.id,
            exchange=api_key.exchange,
            exchange_order_id=placed.exchange_order_id,
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            order_type="market",
            status="open",
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            signal_source=f"strategy:{agent.strategy.type}" if agent.strategy else "strategy",
            signal_data={
                "confidence": signal.confidence,
                "reason": signal.reason,
                # Decision Ledger — immutable snapshot of why this trade was opened
                "ai_available": (decision_context or {}).get("ai_available", True),
                "sl_source": (decision_context or {}).get("sl_source", "unknown"),
                "tp_source": (decision_context or {}).get("tp_source", "unknown"),
                "recovery_mode_at_entry": (decision_context or {}).get("recovery_mode", False),
                "strategy_params_snapshot": (decision_context or {}).get("strategy_params_snapshot", {}),
                **(signal.metadata or {}),
            },
            # Derive market_regime: prefer AI's market_structure tag, fall back to
            # the strategy's trend-filter tag saved in signal metadata.
            market_regime=(
                (signal.metadata or {}).get("market_structure")
                or (signal.metadata or {}).get("market_regime")
            ) or None,
            risk_checks=risk_payload,
            opened_at=datetime.now(timezone.utc),
        )
        await trade.insert()
        logger.info("agent {} {} persisted trade doc id={}", agent.id, symbol, trade.id)

        position = Position(
            user_id=agent.user_id,
            agent_id=agent.id,
            trade_id=trade.id,
            exchange=api_key.exchange,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
        await position.insert()
        logger.info("agent {} {} persisted position doc id={}", agent.id, symbol, position.id)

        agent.total_trades = (agent.total_trades or 0) + 1
        agent.last_trade_at = datetime.now(timezone.utc)
        agent.confidence_score = max(0, min(100, 50 + (signal.confidence - 0.5) * 100))
        if agent.recovery_mode:
            agent.recovery_mode = False
            logger.info("agent {} recovery trade opened — recovery mode cleared", agent.id)

        # Session trade counter — triggers wind-down after N trades
        agent.session_trade_count = (agent.session_trade_count or 0) + 1
        await agent.save()
        logger.info("agent {} {} updated counters: total_trades={} session_trade_count={}", agent.id, symbol, agent.total_trades, agent.session_trade_count)

        await NotificationService.create(
            user_id=agent.user_id,
            type="trade_opened",
            title=f"{agent.name} opened {side} {symbol}",
            message=(
                f"qty {quantity:.6f} @ {entry_price:.4f}, "
                f"SL {sl_price:.4f}, TP {tp_price:.4f}"
            ),
            data={"agent_id": str(agent.id), "trade_id": str(trade.id)},
        )

    async def _open_trade(
        self,
        *,
        agent: Agent,
        api_key,
        client,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal,
        risk_payload: dict[str, Any],
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        min_qty: float = 0.0,
        decision_context: dict[str, Any] | None = None,
    ) -> None:
        # Allow callers to pass explicit rounded prices (recommended).
        sl_price = (
            stop_loss_price
            if stop_loss_price is not None
            else (entry_price * (1 - stop_loss_pct / 100) if side == "long" else entry_price * (1 + stop_loss_pct / 100))
        )
        tp_price = (
            take_profit_price
            if take_profit_price is not None
            else (entry_price * (1 + take_profit_pct / 100) if side == "long" else entry_price * (1 - take_profit_pct / 100))
        )

        order = OrderRequest(
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            order_type="market",
            quantity=quantity,
            stop_loss=sl_price,
            take_profit=tp_price,
            client_order_id=f"agent-{agent.id}-{uuid.uuid4().hex[:8]}",
        )

        if agent.is_paper_trade:
            import random as _rand
            # Simulate market impact and bid-ask spread (0.02–0.06% each side)
            _slippage_pct = _rand.uniform(0.0002, 0.0006)
            simulated_fill = (
                entry_price * (1 + _slippage_pct)
                if order.side == "buy"
                else entry_price * (1 - _slippage_pct)
            )
            # Simulate exchange acknowledgement latency (0.3–1.5 seconds)
            await asyncio.sleep(_rand.uniform(0.3, 1.5))
            logger.info(
                "agent {} {} paper fill: qty={} entry={} fill={} slippage={:.4f}%",
                agent.id, symbol, quantity, entry_price, simulated_fill, _slippage_pct * 100,
            )
            placed = OrderResult(
                exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
                status="filled",
                avg_fill_price=simulated_fill,
                filled_qty=quantity,
                raw={"paper": True, "slippage_pct": round(_slippage_pct * 100, 4)},
            )
        else:
            try:
                # Safety: ensure live orders always include SL and TP
                if not agent.is_paper_trade and (order.stop_loss is None or order.take_profit is None):
                    agent.last_error = "live orders must include stop_loss and take_profit"
                    await RiskEngine.log_risk_event(
                        user_id=agent.user_id,
                        agent_id=agent.id,
                        event_type="order_validation",
                        severity="critical",
                        message="order rejected: missing SL/TP for live order",
                        details={"symbol": symbol, "side": side},
                    )
                    logger.warning("agent {} {} live order rejected: missing SL/TP", agent.id, symbol)
                    return

                # Set target leverage then verify what Bitget actually applied.
                target_leverage = 10 if float(agent.assigned_capital or 0) <= 50 else 5
                try:
                    if hasattr(client, "set_leverage"):
                        await client.set_leverage(symbol, target_leverage, side=side)
                except Exception:
                    pass

                # Read the leverage Bitget actually has set (may differ from target).
                actual_leverage = 1
                if hasattr(client, "get_account_leverage"):
                    actual_leverage = await client.get_account_leverage(symbol)
                if actual_leverage < 1:
                    actual_leverage = 1

                # Cap notional to what the available balance can support at actual leverage.
                try:
                    balances = await client.get_balances()
                    avail = max((b.available for b in balances), default=0.0)
                    max_notional = avail * actual_leverage * 0.70  # 30% buffer for fees/margin/Bitget internal reserve
                    notional = quantity * entry_price
                    logger.info(
                        "agent {} {} balance check: avail=${:.4f} target_lev={}x"
                        " actual_lev={}x max_notional=${:.2f} order_notional=${:.2f}",
                        agent.id, symbol, avail, target_leverage, actual_leverage,
                        max_notional, notional,
                    )
                    # Hard cap: never commit more than 25% of balance as margin
                    # on one trade. margin = notional / leverage, so:
                    # max_notional = avail * 0.25 * leverage
                    # This allows full leverage use while protecting the account
                    # from all-in positions at low leverage.
                    max_notional = min(max_notional, avail * actual_leverage * 0.25)

                    _BITGET_MIN_NOTIONAL = 5.0  # Bitget minimum order size in USDT
                    if max_notional < _BITGET_MIN_NOTIONAL:
                        msg = (
                            f"insufficient free margin: ${avail:.4f} available "
                            f"(need >${_BITGET_MIN_NOTIONAL / actual_leverage:.2f} free for min order at {actual_leverage}x). "
                            f"Close open positions or deposit more funds."
                        )
                        agent.last_error = msg
                        await agent.save()
                        logger.warning(
                            "agent {} {} skipping order: max_notional=${:.2f} below"
                            " Bitget minimum ${:.2f} (avail=${:.4f} actual_lev={}x)",
                            agent.id, symbol, max_notional, _BITGET_MIN_NOTIONAL,
                            avail, actual_leverage,
                        )
                        return
                    if notional > max_notional:
                        capped_qty = max_notional / entry_price
                        logger.info(
                            "agent {} {} capping qty {:.4f}→{:.4f} (${:.2f}→${:.2f} at {}x)",
                            agent.id, symbol, quantity, capped_qty, notional,
                            max_notional, actual_leverage,
                        )
                        order.quantity = capped_qty
                        quantity = capped_qty
                        notional = max_notional
                    # After capping, verify qty still meets exchange minimum lot size.
                    if min_qty > 0 and quantity < min_qty:
                        logger.warning(
                            "agent {} {} skipping: capped qty {:.6f} < exchange min {:.6f}"
                            " (notional ${:.2f} at {}x — need more capital or higher leverage)",
                            agent.id, symbol, quantity, min_qty, notional, actual_leverage,
                        )
                        return
                except ExchangeError as exc:
                    logger.warning("agent {} {} balance check failed: {} — skipping order", agent.id, symbol, exc)
                    return
                except Exception as exc:
                    logger.warning("agent {} {} balance check error: {}", agent.id, symbol, exc)

                logger.info("agent {} {} sending live order: qty={} side={} sl={} tp={}", agent.id, symbol, quantity, side, sl_price, tp_price)
                placed = await client.place_order(order)
                logger.info("agent {} {} live order response: status={} order_id={}", agent.id, symbol, getattr(placed, "status", None), getattr(placed, "exchange_order_id", None))
            except ExchangeError as exc:
                agent.last_error = f"order failed: {exc}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="api_error",
                    severity="critical",
                    message=f"order placement failed: {exc}",
                    details={"symbol": symbol, "side": side},
                )
                logger.error("agent {} {} order placement failed: {}", agent.id, symbol, exc)
                return
            except Exception as exc:
                logger.exception("agent {} {} unexpected error while placing order: {}", agent.id, symbol, exc)
                return

        logger.info("agent {} {} persisting trade after order placement", agent.id, symbol)
        await self._persist_open_trade(
            agent=agent,
            api_key=api_key,
            placed=placed,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            signal=signal,
            risk_payload=risk_payload,
            decision_context=decision_context,
        )

    async def _close_position(
        self,
        agent: Agent,
        api_key,
        client,
        position: Position,
        last_price: float,
        reason: str,
    ) -> None:
        if not agent.is_paper_trade:
            _max_attempts = 5
            _last_exc: Exception | None = None
            for _attempt in range(1, _max_attempts + 1):
                order = OrderRequest(
                    symbol=position.symbol,
                    side="sell" if position.side == "long" else "buy",
                    order_type="market",
                    quantity=float(position.quantity),
                    reduce_only=True,
                    # Fresh client_order_id each attempt so exchange doesn't reject as duplicate.
                    client_order_id=f"agent-{agent.id}-close-{uuid.uuid4().hex[:8]}",
                )
                try:
                    await client.place_order(order)
                    _last_exc = None
                    break  # success
                except ExchangeError as exc:
                    # 22002 = "No position to close" — exchange already closed it (SL/TP hit).
                    # Stop retrying and fall through to reconcile the DB.
                    if "22002" in str(exc):
                        logger.info(
                            "agent {} {} close order: exchange says no position (22002) — already closed by SL/TP, reconciling DB",
                            agent.id, position.symbol,
                        )
                        _last_exc = None
                        break
                    _last_exc = exc
                    logger.warning(
                        "agent {} {} close attempt {}/{} failed: {} — retrying in 1s",
                        agent.id, position.symbol, _attempt, _max_attempts, exc,
                    )
                    if _attempt < _max_attempts:
                        await asyncio.sleep(1)
                except Exception as exc:
                    _last_exc = exc
                    logger.warning(
                        "agent {} {} close attempt {}/{} failed: {} — retrying in 1s",
                        agent.id, position.symbol, _attempt, _max_attempts, exc,
                    )
                    if _attempt < _max_attempts:
                        await asyncio.sleep(1)

            if _last_exc is not None:
                # All attempts failed — leave position open so the next tick retries.
                agent.last_error = f"close failed after {_max_attempts} attempts: {_last_exc}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="api_error",
                    severity="critical",
                    message=f"position close failed after {_max_attempts} attempts: {_last_exc}",
                    details={"symbol": position.symbol},
                )
                logger.error(
                    "agent {} {} all {} close attempts failed — position left open for next tick: {}",
                    agent.id, position.symbol, _max_attempts, _last_exc,
                )
                return

        entry_price = float(position.entry_price)
        qty = float(position.quantity)
        raw_pnl = (last_price - entry_price) * qty
        if position.side == "short":
            raw_pnl = -raw_pnl
        # Bitget taker fee: 0.06% per side = 0.12% round-trip on notional.
        # Deduct from every close so displayed P&L matches actual balance change.
        _fee_rate = 0.00120
        fees = entry_price * qty * _fee_rate
        gross = raw_pnl - fees

        position.is_open = False
        position.current_price = last_price
        position.unrealized_pnl = gross
        position.unrealized_pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
        position.updated_at = datetime.now(timezone.utc)
        await position.save()

        trade = None
        if position.trade_id is not None:
            try:
                trade = await Trade.find_one(Trade.id == position.trade_id)
            except Exception:
                trade = None

        if trade is None:
            trade = await Trade.find_one(
                Trade.agent_id == agent.id,
                Trade.symbol == position.symbol,
                Trade.status == "open",
                Trade.side == ("buy" if position.side == "long" else "sell"),
                Trade.entry_price == entry_price,
            )
            if trade:
                logger.warning(
                    "agent {} closing position {}: fallback matched open trade id={} by agent/symbol/entry",
                    agent.id,
                    position.id,
                    trade.id,
                )
                if position.trade_id != trade.id:
                    position.trade_id = trade.id
                    await position.save()

        if trade is None:
            trade = await Trade.find_one(
                Trade.agent_id == agent.id,
                Trade.symbol == position.symbol,
                Trade.status == "open",
            )
            if trade:
                logger.warning(
                    "agent {} closing position {}: fallback matched open trade id={} by agent/symbol",
                    agent.id,
                    position.id,
                    trade.id,
                )
                if position.trade_id != trade.id:
                    position.trade_id = trade.id
                    await position.save()

        if trade:
            trade.exit_price = last_price
            trade.pnl = gross
            trade.pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
            trade.status = "filled"
            trade.closed_at = datetime.now(timezone.utc)
            trade.notes = reason
            await trade.save()
        else:
            logger.warning(
                "agent {} closing position {}: no matching trade record found for trade_id=%s symbol=%s",
                agent.id,
                position.trade_id,
                position.symbol,
            )

        agent.total_pnl = float(agent.total_pnl or 0) + gross
        agent.current_day_pnl = float(agent.current_day_pnl or 0) + gross
        agent.current_week_pnl = float(agent.current_week_pnl or 0) + gross
        if gross > 0:
            agent.winning_trades = (agent.winning_trades or 0) + 1
            agent.recovery_mode = False  # winning trade clears recovery mode
            agent.pause_cycle_count = 0  # reset pause cycle count on any win
        await agent.save()

        # Feed the outcome back into the fleet learning system so other agents
        # can learn from this trade's success or failure.
        try:
            from app.services.learning import LearningService
            strategy_type = agent.strategy.type if agent.strategy else None
            if strategy_type:
                await LearningService.record_trade_outcome(
                    agent_id=agent.id,
                    strategy_type=strategy_type,
                    symbol=position.symbol,
                    timeframe=agent.timeframe,
                    pnl=gross,
                )
        except Exception:
            pass

        await NotificationService.create(
            user_id=agent.user_id,
            type="trade_closed",
            title=f"{agent.name} closed {position.side} {position.symbol}",
            message=f"PnL {gross:+.2f} ({reason})",
            data={"agent_id": str(agent.id), "trade_id": str(position.trade_id)},
        )

        # After consecutive losses, trigger recovery/pause.
        if gross < 0:
            await self._check_loss_streak_and_recover(agent, position.symbol)

    async def _check_loss_streak_and_recover(self, agent: Agent, symbol: str) -> None:
        """Called after every losing trade close (all paths). Counts streak and
        either enters recovery mode (half size) or pauses the agent entirely.

        Crucially, only losses AFTER last_resumed_at are counted.  Without this
        boundary, pre-pause losses accumulate across auto-resume cycles and re-
        trigger a pause on the very first post-resume loss — infinite loop.
        """
        # Only count losses that happened after the most recent resume.
        # This gives the agent a clean streak slate each time it wakes up.
        query_filters = [
            Trade.agent_id == agent.id,
            Trade.status == "filled",
            Trade.closed_at != None,  # noqa: E711
        ]
        last_resumed = getattr(agent, "last_resumed_at", None)
        if last_resumed:
            if last_resumed.tzinfo is None:
                last_resumed = last_resumed.replace(tzinfo=timezone.utc)
            query_filters.append({"closed_at": {"$gte": last_resumed}})

        streak_trades = await Trade.find(*query_filters).sort(-Trade.closed_at).limit(10).to_list()

        loss_streak = 0
        for t in streak_trades:
            if float(t.pnl or 0) < 0:
                loss_streak += 1
            else:
                break

        logger.info(
            "agent {} loss streak after close: {} (since last_resumed_at={})",
            agent.id, loss_streak, last_resumed,
        )

        from app.config import settings  # local import avoids circular dependency
        max_losses = getattr(settings, "MAX_CONSECUTIVE_LOSSES", 3)

        if loss_streak >= max_losses * 2:
            # Double the limit → pause trading entirely.
            # Auto-resume will re-optimize and reset last_resumed_at so this
            # streak counter starts fresh from 0 after the cooldown.
            if agent.status == "active":
                agent.status = "paused"
                agent.recovery_mode = True
                agent.pause_cycle_count = int(getattr(agent, "pause_cycle_count", 0) or 0) + 1
                await agent.save()
                logger.warning(
                    "agent {} PAUSED after {} consecutive post-resume losses (pause_cycle={})",
                    agent.id, loss_streak, agent.pause_cycle_count,
                )
                await NotificationService.create(
                    user_id=agent.user_id,
                    type="agent_status",
                    title=f"{agent.name} paused — {loss_streak} losses in a row",
                    message=f"Will auto-resume in 30 min with re-optimized params. Pause cycle #{agent.pause_cycle_count}.",
                    data={"agent_id": str(agent.id), "trigger": "loss_streak_pause", "streak": loss_streak},
                )
        elif loss_streak >= max_losses:
            # At the limit → recovery mode (half size) + re-optimize.
            agent.recovery_mode = True
            await agent.save()
            try:
                await self._emergency_optimize(agent, symbol, loss_streak)
            except Exception as exc:
                logger.warning("emergency optimize failed for {}: {}", agent.id, exc)

    async def _emergency_optimize(self, agent: Agent, symbol: str, loss_streak: int) -> None:
        """Re-optimize after consecutive losses. Agent is already in recovery_mode.

        Studies fleet winners, re-optimizes with Bayesian search, and tightens params.
        """
        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type or not agent.api_key_id:
            return

        logger.warning(
            "agent {} — studying fleet winners and re-optimizing after {} losses",
            agent.id, loss_streak,
        )

        from app.models.api_key import ApiKey
        from app.services.exchange import build_client
        from app.services.strategy import get_strategy
        from app.services.strategy.indicators import candles_to_df
        from app.services.ai_optimizer import optimize_strategy_async
        from app.services.learning import LearningService

        api_key = await ApiKey.get(agent.api_key_id)
        if not api_key:
            return

        strat = get_strategy(strategy_type)
        client = build_client(api_key)
        try:
            candles = await client.get_candles(symbol, agent.timeframe, limit=400)
        finally:
            await client.close()

        if len(candles) < 100:
            return

        df = candles_to_df(candles)

        # Study what other agents are doing well — pull fleet-wide winners
        warm_starts = await LearningService.warm_starts(
            strategy_type=strategy_type,
            symbol=symbol,
            timeframe=agent.timeframe,
        )

        # Also look for the best-performing agent on this strategy to learn from
        best_fleet = await LearningService.fleet_best(
            strategy_type=strategy_type, symbol=symbol, limit=3,
        )
        # If fleet has proven winning params with positive realized PnL, prefer those
        fleet_winner_params = None
        for obs in best_fleet:
            if float(obs.realized_pnl or 0) > 0 and int(obs.realized_trades or 0) >= 3:
                fleet_winner_params = dict(obs.params)
                logger.info(
                    "agent {} adopting fleet winner params (realized_pnl={:.2f}, trades={}): {}",
                    agent.id, obs.realized_pnl, obs.realized_trades, obs.params,
                )
                break

        # Start from fleet winner params if available, otherwise from current + defaults
        if fleet_winner_params:
            base = {**(strat.default_params or {}), **fleet_winner_params}
        else:
            base = {**(strat.default_params or {}), **(agent.strategy_params or {})}

        result = await optimize_strategy_async(
            strat, df, base,
            symbol=symbol,
            timeframe=agent.timeframe,
            warm_starts=warm_starts,
            n_calls=15,
        )

        # Apply tighter risk overrides on top of optimized params.
        # Use the strategy's own default SL as the ceiling — never let the
        # optimizer widen SL beyond what the strategy was designed for.
        new_params = dict(result.best_params)
        strategy_default_sl = float((strat.default_params or {}).get("stop_loss_pct", 0.5))
        if "stop_loss_pct" in new_params:
            new_params["stop_loss_pct"] = min(new_params["stop_loss_pct"], strategy_default_sl)
        if "profit_target_pct" in new_params:
            strategy_default_tp = float((strat.default_params or {}).get("profit_target_pct", 2.5))
            # Floor: TP must cover round-trip fees + margin; cap at 2× strategy default.
            new_params["profit_target_pct"] = max(
                min(new_params["profit_target_pct"], strategy_default_tp * 2),
                0.25,
            )
        if "min_confidence" in new_params:
            new_params["min_confidence"] = max(new_params["min_confidence"], 0.50)
        if "position_size_pct" in new_params:
            new_params["position_size_pct"] = min(new_params["position_size_pct"], 1.5)
        # Cap deviation_pct so the optimizer can't make the signal impossible to trigger.
        if "deviation_pct" in new_params:
            strategy_default_dev = float((strat.default_params or {}).get("deviation_pct", 0.5))
            new_params["deviation_pct"] = min(new_params["deviation_pct"], strategy_default_dev * 3)

        agent.strategy_params = new_params
        agent.recovery_mode = True  # halves position size until a winning trade
        agent.optimization_params = {
            "score": result.best_score,
            "trigger": "emergency_loss_streak",
            "loss_streak": loss_streak,
            "iterations": result.iterations,
            "adopted_fleet_winner": fleet_winner_params is not None,
        }
        await agent.save()

        # Propagate winning params to the entire fleet
        try:
            await LearningService.propagate_to_fleet(
                strategy_type=strategy_type,
                winning_params=new_params,
                source_agent_id=agent.id,
            )
        except Exception:
            pass

        logger.info(
            "agent {} emergency re-optimized + propagated to fleet: score={:.4f} params={}",
            agent.id, result.best_score, new_params,
        )

        await NotificationService.create(
            user_id=agent.user_id,
            type="agent_optimized",
            title=f"{agent.name} recovering after {loss_streak} losses",
            message=(
                f"Studied fleet winners, re-optimized params (score: {result.best_score:.3f}). "
                f"Now in recovery mode — next trade at half size to prove new params work."
            ),
            data={"agent_id": str(agent.id), "trigger": "loss_streak", "recovery": True},
        )

    async def _study_fleet_and_adapt(self, agent: Agent) -> None:
        """Called when a session cooldown ends. Study what other agents learned
        and adopt better params if available."""
        strategy_type = agent.strategy.type if agent.strategy else None
        if not strategy_type:
            return

        from app.services.learning import LearningService

        best = await LearningService.fleet_best(
            strategy_type=strategy_type, limit=5,
        )

        # Find the observation with the best realized PnL (not just backtest)
        winner = None
        for obs in best:
            if float(obs.realized_pnl or 0) > 0 and int(obs.realized_trades or 0) >= 3:
                winner = obs
                break

        if winner:
            old_params = dict(agent.strategy_params or {})
            new_params = {**old_params, **winner.params}
            agent.strategy_params = new_params
            await agent.save()
            logger.info(
                "agent {} adopted fleet winner params after cooldown study (realized_pnl={:.2f}): {}",
                agent.id, winner.realized_pnl, winner.params,
            )
            # Share the winning params with all agents using this strategy
            try:
                await LearningService.propagate_to_fleet(
                    strategy_type=strategy_type,
                    winning_params=new_params,
                    source_agent_id=agent.id,
                )
            except Exception:
                pass
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_optimized",
                title=f"{agent.name} learned from fleet — shared with all agents",
                message=f"Adopted winning params (realized PnL: ${winner.realized_pnl:.2f}). Propagated to all {strategy_type} agents.",
                data={"agent_id": str(agent.id), "trigger": "cooldown_study"},
            )
        else:
            logger.info("agent {} cooldown study: no fleet winner found, keeping current params", agent.id)
