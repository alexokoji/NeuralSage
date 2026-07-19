"""Pure async job functions — MongoDB/Beanie edition.

Both the in-process APScheduler (app/scheduler.py) and the optional
Celery worker (app/workers/*) call these — keeping the business logic
in one place regardless of how it's dispatched.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from loguru import logger

from app.models.agent import Agent
from app.models.agent_performance import AgentPerformance
from app.models.api_key import ApiKey
from app.models.position import Position
from app.models.trade import Trade
from app.services.ai_optimizer import optimize_strategy_async
import app.services.grok_analyst as grok_analyst
from app.services.exchange import build_client
from app.services.strategy_guardian import snapshot_params as _guardian_snapshot
from app.services.pnl_watchdog import run_pnl_watchdog
from app.services.strategy_guardian import run_strategy_guardian
from app.services.news_sentinel import run_news_sentinel
from app.services.learning import LearningService
from app.services.strategy import get_strategy
from app.services.strategy.indicators import candles_to_df
from app.services.trading_engine import TradingEngine


# --------------------------------------------------------------------------- #
# Trading tick — runs every TRADE_LOOP_INTERVAL_SECONDS
# --------------------------------------------------------------------------- #


_PAUSE_COOLDOWN_MINUTES = 30  # auto-resume paused agents after this many minutes


async def _auto_resume_paused_agents() -> None:
    """Re-activate agents that have been paused for >= PAUSE_COOLDOWN_MINUTES.

    On each resume we:
      1. Run a fresh fleet study + re-optimization so the agent wakes up with
         better params rather than the same ones that just lost 6 times.
      2. Set last_resumed_at so the loss-streak counter starts fresh from 0 —
         without this, pre-pause losses still count and re-trigger a pause on
         the very first post-resume loss (infinite loop).
      3. Scale position size by pause_cycle_count: 1st pause→half, 2nd→quarter,
         so the account bleeds slower if the market is persistently unfavourable.
    """
    from datetime import datetime, timezone, timedelta
    try:
        paused = await Agent.find({"status": "paused"}).to_list()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=_PAUSE_COOLDOWN_MINUTES)
        for agent in paused:
            last_active = getattr(agent, "last_tick_at", None) or getattr(agent, "updated_at", None)
            if last_active and last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            if last_active is None or last_active > cutoff:
                continue  # paused too recently — wait longer

            # --- Re-optimize before resuming so the agent has fresh params ---
            try:
                await _study_and_reoptimize_for_resume(agent)
            except Exception as exc:
                logger.warning("agent {} resume re-optimize failed: {}", agent.id, exc)

            # Reset the streak boundary — only losses AFTER this timestamp count.
            agent.last_resumed_at = now
            agent.status = "active"
            agent.recovery_mode = True  # half-size trades until first win

            cycle = int(getattr(agent, "pause_cycle_count", 0) or 0)
            logger.info(
                "agent {} auto-resumed after {}min cooldown (cycle={} recovery_mode=True last_resumed_at={})",
                agent.id, _PAUSE_COOLDOWN_MINUTES, cycle, now.isoformat(),
            )

            from app.services.notifications import NotificationService
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} resumed after cooldown (cycle #{cycle})",
                message="Re-optimized with fleet data. Trading at reduced size until a win confirms new params.",
                data={"agent_id": str(agent.id), "trigger": "auto_resume", "cycle": cycle},
            )
            await agent.save()
    except Exception as exc:
        logger.warning("auto-resume check failed: {}", exc)


async def _study_and_reoptimize_for_resume(agent: Agent) -> None:
    """Fleet study + loss-aware Bayesian re-optimization before auto-resume.

    Three stages:
      1. Collect recent losing trades and summarise WHY they lost.
      2. Run Bayesian optimizer with that loss context fed into the Grok
         prompt so the AI can reason "these longs all failed in a downtrend,
         try higher min_confidence / tighter SL."
      3. Backtest gate: if the best params still lose on recent candles,
         raise ValueError so the caller extends the cooldown rather than
         waking the agent up with params that are already known to fail.
    """
    strategy_type = agent.strategy.type if agent.strategy else None
    if not strategy_type or not agent.api_key_id:
        return

    api_key = await ApiKey.get(agent.api_key_id)
    if not api_key:
        return

    strat = get_strategy(strategy_type)
    client = build_client(api_key)

    symbol = (agent.trading_pairs or ["BTCUSDT"])[0]
    try:
        candles = await client.get_candles(symbol, agent.timeframe, limit=400)
    finally:
        await client.close()

    if len(candles) < 100:
        return

    df = candles_to_df(candles)

    # ── 1. Collect recent losing trades as learning context ───────────────
    recent_losses = await Trade.find(
        Trade.agent_id == agent.id,
        Trade.status == "filled",
        {"pnl": {"$lt": 0}},
    ).sort(-Trade.closed_at).limit(10).to_list()

    loss_context = [
        {
            "symbol": t.symbol,
            "side": t.side,
            "entry_price": float(t.entry_price or 0),
            "exit_price": float(t.exit_price or 0),
            "pnl": float(t.pnl or 0),
            "pnl_pct": float(t.pnl_pct or 0),
            "signal_reason": (t.signal_data or {}).get("reason", ""),
            "confidence": (t.signal_data or {}).get("confidence", ""),
            "deviation_pct": (t.signal_data or {}).get("deviation_pct", ""),
            "opened_at": t.opened_at.isoformat() if t.opened_at else "",
            "closed_at": t.closed_at.isoformat() if t.closed_at else "",
        }
        for t in recent_losses
    ] if recent_losses else None

    logger.info(
        "agent {} resume study: {} recent losses to feed optimizer",
        agent.id, len(recent_losses),
    )

    # ── 2. Fleet warm-starts ──────────────────────────────────────────────
    warm_starts = await LearningService.warm_starts(
        strategy_type=strategy_type,
        symbol=symbol,
        timeframe=agent.timeframe,
    )
    best_fleet = await LearningService.fleet_best(
        strategy_type=strategy_type, symbol=symbol, limit=5,
    )

    fleet_winner_params = None
    for obs in best_fleet:
        if float(obs.realized_pnl or 0) > 0 and int(obs.realized_trades or 0) >= 2:
            fleet_winner_params = dict(obs.params)
            logger.info(
                "agent {} resume: adopting fleet winner params (pnl={:.2f} trades={})",
                agent.id, obs.realized_pnl, obs.realized_trades,
            )
            break

    base = (
        {**(strat.default_params or {}), **fleet_winner_params}
        if fleet_winner_params
        else {**(strat.default_params or {}), **(agent.strategy_params or {})}
    )

    result = await optimize_strategy_async(
        strat, df, base,
        symbol=symbol,
        timeframe=agent.timeframe,
        warm_starts=warm_starts,
        n_calls=20,
        loss_context=loss_context,
    )

    new_params = dict(result.best_params)

    # Safety bounds
    strategy_default_sl = float((strat.default_params or {}).get("stop_loss_pct", 0.5))
    if "stop_loss_pct" in new_params:
        new_params["stop_loss_pct"] = min(new_params["stop_loss_pct"], strategy_default_sl)
    if "profit_target_pct" in new_params:
        strategy_default_tp = float((strat.default_params or {}).get("profit_target_pct", 2.5))
        new_params["profit_target_pct"] = max(
            min(new_params["profit_target_pct"], strategy_default_tp * 2), 0.25,
        )
    if "min_confidence" in new_params:
        # Floor at 0.50 — higher kills too many valid signals on a small pair set.
        # The trend filter and AI gate already protect against low-quality entries.
        new_params["min_confidence"] = max(new_params["min_confidence"], 0.50)
    if "deviation_pct" in new_params:
        strategy_default_dev = float((strat.default_params or {}).get("deviation_pct", 0.5))
        new_params["deviation_pct"] = min(new_params["deviation_pct"], strategy_default_dev * 3)

    cycle = int(getattr(agent, "pause_cycle_count", 0) or 0)
    default_size = float((strat.default_params or {}).get("position_size_pct", 1.5))
    if cycle >= 2:
        new_params["position_size_pct"] = round(default_size * 0.25, 3)
        logger.warning("agent {} cycle {} — position size quartered to {:.3f}%",
                       agent.id, cycle, new_params["position_size_pct"])
    else:
        new_params["position_size_pct"] = round(default_size * 0.5, 3)

    # ── 3. Backtest gate — don't resume if params still lose on recent data ─
    from app.services.backtester import backtest as run_backtest
    backtest_check = run_backtest(strat, df, new_params)
    if backtest_check.score <= 0:
        # Market conditions are unfavourable for this strategy right now.
        # Update last_tick_at so the cooldown timer resets and we try again
        # in another _PAUSE_COOLDOWN_MINUTES minutes.
        from datetime import datetime, timezone
        agent.last_tick_at = datetime.now(timezone.utc)
        await agent.save()
        logger.warning(
            "agent {} backtest gate FAILED (score={:.4f} trades={} win_rate={:.0%})"
            " — extending cooldown, not resuming yet",
            agent.id, backtest_check.score, backtest_check.trades, backtest_check.win_rate,
        )
        raise ValueError(
            f"backtest gate: optimized params score {backtest_check.score:.4f} ≤ 0 "
            f"on recent {len(df)} candles — market conditions unfavourable for {strategy_type}"
        )

    # Snapshot before applying so guardian can revert if new params hurt
    _guardian_snapshot(agent)
    agent.strategy_params = new_params
    agent.optimization_params = {
        "score": result.best_score,
        "backtest_score": backtest_check.score,
        "backtest_trades": backtest_check.trades,
        "backtest_win_rate": round(backtest_check.win_rate, 3),
        "trigger": "resume_study",
        "pause_cycle": cycle,
        "adopted_fleet_winner": fleet_winner_params is not None,
        "iterations": result.iterations,
        "loss_trades_analysed": len(recent_losses),
    }

    logger.info(
        "agent {} resume re-optimized: optimizer_score={:.4f} backtest_score={:.4f}"
        " cycle={} losses_analysed={} params={}",
        agent.id, result.best_score, backtest_check.score,
        cycle, len(recent_losses), new_params,
    )


async def run_trading_tick_for_all_agents() -> dict:
    """Iterate every active agent and run one trading tick."""
    processed = 0
    skipped = 0
    failed = 0

    logger.debug("trading tick starting")

    # Auto-resume agents that were paused long enough.
    await _auto_resume_paused_agents()

    try:
        agent_ids = [
            a.id
            for a in await Agent.find(Agent.status == "active").to_list()
        ]
    except Exception as exc:  # noqa: BLE001
        logger.exception("trading tick: failed to query active agents: {}", exc)
        return {"processed": 0, "skipped": 0, "failed": 0, "db_error": str(exc)}

    logger.debug("trading tick: found {} active agent(s)", len(agent_ids))

    engine = TradingEngine()
    for agent_id in agent_ids:
        try:
            agent = await Agent.get(agent_id)
            if not agent or agent.status != "active" or not agent.api_key_id:
                skipped += 1
                continue
            api_key = await ApiKey.get(agent.api_key_id)
            if not api_key:
                skipped += 1
                continue

            await engine.run_agent_tick(agent, api_key)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("trading tick failed for agent {}: {}", agent_id, exc)

    logger.info(
        "trading tick complete: processed={} skipped={} failed={}",
        processed,
        skipped,
        failed,
    )
    return {"processed": processed, "skipped": skipped, "failed": failed}


async def reconcile_exchange_positions() -> dict:
    """Compare DB positions with the exchange and repair stale/missing state.

    This is meant to keep the agent's internal position history trustworthy
    after exchange-side closes, order rejections, or reconnects.
    """
    repaired = 0
    skipped = 0

    try:
        agents = await Agent.find(Agent.status == "active").to_list()
    except Exception as exc:  # noqa: BLE001
        logger.exception("exchange reconciliation failed to query agents: {}", exc)
        return {"repaired": 0, "skipped": 0, "failed": True, "error": str(exc)}

    for agent in agents:
        try:
            if not agent.api_key_id:
                skipped += 1
                continue
            api_key = await ApiKey.get(agent.api_key_id)
            if not api_key:
                skipped += 1
                continue

            client = build_client(api_key)
            try:
                positions = await Position.find(
                    Position.agent_id == agent.id,
                    Position.is_open == True,  # noqa: E712
                ).to_list()
                if not positions:
                    continue

                # Query exchange for currently open positions.
                # IMPORTANT: if unsupported or failed, skip this agent entirely
                # rather than treating all DB positions as exchange-closed.
                exchange_query_ok = False
                exchange_positions: list = []
                try:
                    exchange_positions = await client.get_positions() or []
                    exchange_query_ok = True
                except AttributeError:
                    logger.debug(
                        "agent {} exchange {} does not support get_positions — skipping reconciliation",
                        agent.id, api_key.exchange,
                    )
                except Exception as exc:
                    logger.debug("agent {} exchange position query failed: {}", agent.id, exc)

                if not exchange_query_ok:
                    skipped += 1
                    continue

                exchange_symbols = {
                    str(item.get("symbol") or "").upper()
                    for item in exchange_positions
                    if item and str(item.get("symbol") or "").strip()
                }

                # Pre-fetch recent closed orders once per agent to find actual fill prices.
                # Look back 24 h so we catch any SL/TP that fired since the last reconcile.
                closed_orders_by_symbol: dict[str, list[dict]] = {}
                try:
                    import time as _time
                    lookback_ms = int((_time.time() - 86_400) * 1000)
                    raw_closed = await client.get_closed_orders(limit=100, start_ms=lookback_ms)
                    for o in raw_closed:
                        sym = o["symbol"]
                        closed_orders_by_symbol.setdefault(sym, []).append(o)
                except AttributeError:
                    pass  # exchange client doesn't support get_closed_orders — fall back to candle price
                except Exception as exc:
                    logger.debug("agent {} closed-order fetch failed: {}", agent.id, exc)

                for pos in positions:
                    if str(pos.symbol).upper() not in exchange_symbols:
                        # Guard: skip positions younger than 2 minutes — Bitget's
                        # position API may not reflect a just-placed market order yet.
                        try:
                            from datetime import timezone as _tz
                            opened = pos.opened_at
                            if opened is not None:
                                if opened.tzinfo is None:
                                    opened = opened.replace(tzinfo=_tz.utc)
                                age_secs = (datetime.now(_tz.utc) - opened).total_seconds()
                            else:
                                age_secs = 999
                        except Exception:
                            age_secs = 999
                        if age_secs < 120:
                            logger.info(
                                "agent {} reconcile: skipping {:.0f}s-old {} position"
                                " (Bitget API not yet showing new order)",
                                agent.id, age_secs, pos.symbol,
                            )
                            continue
                        # Exchange closed this position (SL/TP triggered, liquidation, etc.)
                        entry_price = float(pos.entry_price)
                        qty = float(pos.quantity or 0)

                        # Try to match the actual exit fill from closed orders.
                        # Match on symbol + roughly matching qty, pick the most recent.
                        exchange_order_id = str(getattr(pos, "exchange_order_id", "") or "")
                        actual_fill: dict | None = None
                        sym_orders = closed_orders_by_symbol.get(str(pos.symbol).upper(), [])
                        # 1. Try exact client_order_id match via linked trade
                        trade_for_match = None
                        if pos.trade_id:
                            try:
                                trade_for_match = await Trade.find_one(Trade.id == pos.trade_id)
                            except Exception:
                                pass
                        linked_oid = str(
                            (trade_for_match.exchange_order_id if trade_for_match else None) or exchange_order_id
                        )
                        if linked_oid:
                            actual_fill = next(
                                (o for o in sym_orders if o["order_id"] == linked_oid),
                                None,
                            )
                        # 2. Fall back: pick the most recent filled order on this symbol
                        #    whose qty is within 5 % of the position qty.
                        if actual_fill is None and sym_orders:
                            qty_matches = [
                                o for o in sym_orders
                                if abs(o["filled_qty"] - qty) / max(qty, 1e-9) < 0.05
                            ]
                            if qty_matches:
                                actual_fill = max(qty_matches, key=lambda o: o["closed_at_ms"])

                        if actual_fill and actual_fill["avg_fill_price"] > 0:
                            exit_price = actual_fill["avg_fill_price"]
                            # Use exchange-reported realised PnL when available (includes fees).
                            if actual_fill["pnl"] != 0:
                                gross = actual_fill["pnl"]
                            else:
                                gross = (exit_price - entry_price) * qty
                                if pos.side == "short":
                                    gross = -gross
                            price_source = "exchange_fill"
                        else:
                            # No fill data found — fall back to last known candle price.
                            exit_price = float(pos.current_price or entry_price)
                            gross = (exit_price - entry_price) * qty
                            if pos.side == "short":
                                gross = -gross
                            price_source = "candle_estimate"

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
                                    trade.notes = (
                                        f"{trade.notes or ''} reconciled as closed by exchange"
                                        f" (price_source={price_source})"
                                    ).strip()
                                    await trade.save()
                            except Exception:
                                pass

                        # Update agent P&L counters so dashboards stay current.
                        try:
                            agent_doc = await Agent.get(agent.id)
                            if agent_doc:
                                agent_doc.total_pnl = float(agent_doc.total_pnl or 0) + gross
                                agent_doc.current_day_pnl = float(agent_doc.current_day_pnl or 0) + gross
                                agent_doc.current_week_pnl = float(agent_doc.current_week_pnl or 0) + gross
                                if gross > 0:
                                    agent_doc.winning_trades = (agent_doc.winning_trades or 0) + 1
                                await agent_doc.save()
                        except Exception as exc:
                            logger.debug("agent {} reconcile: failed to update agent stats: {}", agent.id, exc)

                        # Feed outcome into the fleet learning system.
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
                                    current_params=dict(agent.strategy_params or {}),
                                )
                        except Exception:
                            pass

                        # Check loss streak — may trigger recovery mode or pause.
                        # Critical: reconciliation is the fallback when WS misses fills,
                        # so this must mirror what position_stream._handle_fill does.
                        try:
                            from app.services.trading_engine import TradingEngine
                            reconcile_agent = await Agent.get(agent.id)
                            if reconcile_agent:
                                if gross > 0:
                                    reconcile_agent.recovery_mode = False
                                    await reconcile_agent.save()
                                elif gross < 0:
                                    await TradingEngine()._check_loss_streak_and_recover(
                                        reconcile_agent, pos.symbol
                                    )
                        except Exception as exc:
                            logger.debug("agent {} reconcile: streak check failed: {}", agent.id, exc)

                        repaired += 1
                        logger.info(
                            "agent {} reconciled position {} ({}) closed by exchange:"
                            " pnl={:.4f} exit_price={} price_source={}",
                            agent.id,
                            pos.symbol,
                            pos.id,
                            gross,
                            exit_price,
                            price_source,
                        )
            finally:
                await client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("exchange reconciliation failed for agent {}: {}", agent.id, exc)

    logger.info(
        "exchange reconciliation complete: repaired={} skipped={}",
        repaired,
        skipped,
    )
    return {"repaired": repaired, "skipped": skipped, "failed": False}


# --------------------------------------------------------------------------- #
# Keep-alive ping — prevents Render free tier from spinning down
# --------------------------------------------------------------------------- #


async def keep_alive_ping() -> None:
    """Ping the public health endpoint to prevent Render free-tier spin-down.

    Render's idle timer resets on any inbound HTTP request — including a
    self-ping through the public URL. A localhost request does NOT reset it,
    so we always prefer RENDER_EXTERNAL_URL when available.

    Log level is INFO so failures are visible in Render's log dashboard.
    """
    import httpx
    import os
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        url = f"{external_url}/health"
        source = "external"
    else:
        url = f"http://localhost:{settings.APP_PORT}/health"
        source = "localhost (RENDER_EXTERNAL_URL not set — self-ping may not prevent sleep)"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        logger.info("keep-alive ping [{}] {} -> {}", source, url, resp.status_code)
    except Exception as exc:
        logger.warning("keep-alive ping [{}] {} FAILED: {}", source, url, exc)


# --------------------------------------------------------------------------- #
# Optimization sweep — runs every OPTIMIZATION_INTERVAL_HOURS
# --------------------------------------------------------------------------- #


async def run_optimization_sweep() -> dict:
    """Re-tune AI-enabled agents using fleet-wide warm starts."""
    tuned = 0
    seeded_total = 0

    agents = await Agent.find(
        Agent.ai_optimization_enabled == True,  # noqa: E712
        {"status": {"$in": ["active", "paused"]}},
    ).to_list()

    for agent in agents:
        try:
            a = await Agent.get(agent.id)
            if a is None or not a.strategy or not a.api_key_id or not a.trading_pairs:
                continue
            api_key = await ApiKey.get(a.api_key_id)
            if api_key is None:
                continue

            strat = get_strategy(a.strategy.type)
            symbol = a.trading_pairs[0]

            client = build_client(api_key)
            try:
                candles = await client.get_candles(symbol, a.timeframe, limit=400)
            finally:
                await client.close()
            if len(candles) < 100:
                continue
            df = candles_to_df(candles)

            warm_starts = await LearningService.warm_starts(
                strategy_type=a.strategy.type,
                symbol=symbol,
                timeframe=a.timeframe,
            )

            base = {**(strat.default_params or {}), **(a.strategy_params or {})}
            # Skip optimization if guardian has flagged a revert — wait for
            # the agent to stabilize before tuning again.
            if getattr(a, "guardian_verdict", "hold") == "revert":
                logger.info(
                    "optimization_sweep: skipping agent {} — guardian verdict is 'revert'",
                    a.id,
                )
                continue

            result = await optimize_strategy_async(
                strat,
                df,
                base,
                symbol=symbol,
                timeframe=a.timeframe,
                warm_starts=warm_starts,
                n_calls=20,
            )

            # Snapshot before applying new params so guardian can revert if needed
            _guardian_snapshot(a)
            a.strategy_params = result.best_params
            a.optimization_params = {
                "score": result.best_score,
                "iterations": result.iterations,
                "warm_starts_used": result.warm_starts_used,
                "history_tail": result.history[-10:],
            }

            window_start = (
                datetime.fromtimestamp(candles[0].open_time / 1000, tz=timezone.utc)
                if candles
                else None
            )
            window_end = (
                datetime.fromtimestamp(candles[-1].open_time / 1000, tz=timezone.utc)
                if candles
                else None
            )
            await LearningService.record_observation(
                strategy_type=a.strategy.type,
                symbol=symbol,
                timeframe=a.timeframe,
                params=result.best_params,
                backtest_score=result.best_score,
                source_agent_id=a.id,
                source_user_id=a.user_id,
                candle_window_start=window_start,
                candle_window_end=window_end,
            )

            await a.save()
            tuned += 1
            seeded_total += result.warm_starts_used
        except Exception as exc:  # noqa: BLE001
            logger.exception("optimization failed for agent {}: {}", agent.id, exc)

    # --- Propagate best params to the entire fleet ---
    try:
        strategy_types = list({a.strategy.type for a in agents if a.strategy})
        for stype in strategy_types:
            best_obs = await LearningService.fleet_best(strategy_type=stype, limit=1)
            if not best_obs:
                continue
            winner = best_obs[0]
            if float(winner.realized_pnl or 0) <= 0 and int(winner.realized_trades or 0) < 3:
                continue
            await LearningService.propagate_to_fleet(
                strategy_type=stype,
                winning_params=dict(winner.params),
            )
    except Exception as exc:
        logger.warning("fleet param propagation failed: {}", exc)

    logger.info(
        "optimization sweep: tuned={} warm_starts_used_total={}", tuned, seeded_total
    )

    try:
        if agents:
            strategy_types = list({a.strategy.type for a in agents if a.strategy})
            for stype in strategy_types[:2]:
                top = await LearningService.fleet_best(strategy_type=stype, limit=10)
                if top:
                    obs_dicts = [
                        {
                            "params": o.params,
                            "backtest_score": float(o.backtest_score or 0),
                            "realized_pnl": float(o.realized_pnl or 0),
                            "realized_trades": int(o.realized_trades or 0),
                        }
                        for o in top
                    ]
                    insight = await grok_analyst.fleet_insight(obs_dicts, strategy_type=stype)
                    logger.info("Grok fleet insight [{}]: {}", stype, insight)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Grok fleet insight skipped: {}", exc)

    return {"agents_tuned": tuned, "warm_starts_used_total": seeded_total}


# --------------------------------------------------------------------------- #
# Daily rollover — runs at 00:01 UTC
# --------------------------------------------------------------------------- #


async def check_missed_rollover() -> None:
    """Run at startup — if today's rollover hasn't happened yet, reset daily PnL."""
    from app.models.agent_performance import AgentPerformance
    today = date.today()
    existing = await AgentPerformance.find(
        AgentPerformance.snapshot_date == today,
    ).count()
    if existing == 0:
        logger.info("missed daily rollover detected — running now")
        await run_daily_rollover()
    else:
        logger.info("daily rollover already ran today ({} snapshots)", existing)

    # Refresh strategy params for all agents to pick up latest defaults
    await _refresh_agent_strategy_params()

    # Fix agents stuck with old 5% daily loss cap
    agents = await Agent.find_all().to_list()
    for a in agents:
        if float(a.max_daily_loss or 0) < 15:
            a.max_daily_loss = 15.0
            await a.save()
            logger.info("agent {} max_daily_loss raised to 15%", a.name)


async def _refresh_agent_strategy_params() -> None:
    """Push latest strategy default_params into existing agents.

    Agents store strategy_params in the DB. When we update default_params in code,
    existing agents keep the old values. This merges new defaults under agent overrides.
    """
    from app.services.strategy import get_strategy

    agents = await Agent.find_all().to_list()
    updated = 0
    for agent in agents:
        if not agent.strategy or not agent.strategy.type:
            continue
        try:
            strat = get_strategy(agent.strategy.type)
        except KeyError:
            continue

        new_defaults = dict(strat.default_params or {})
        current = dict(agent.strategy_params or {})

        # Only update params that the agent hasn't explicitly customized
        # (i.e., params that still match the OLD defaults or are missing)
        merged = {**new_defaults, **current}

        # Force-update critical screener params if they're too conservative
        if agent.strategy.type == "micro_scalping":
            if float(merged.get("deviation_pct", 1)) > 0.10:
                merged["deviation_pct"] = new_defaults.get("deviation_pct", 0.06)
            if float(merged.get("min_confidence", 1)) > 0.50:
                merged["min_confidence"] = new_defaults.get("min_confidence", 0.45)
            if int(merged.get("ema_period", 99)) > 6:
                merged["ema_period"] = new_defaults.get("ema_period", 5)
            # Raise TP if it's below 0.25% — the old 0.15% barely covered Bitget fees
            if float(merged.get("profit_target_pct", 0)) < 0.25:
                merged["profit_target_pct"] = new_defaults.get("profit_target_pct", 0.30)

        if merged != current:
            agent.strategy_params = merged
            await agent.save()
            updated += 1
            logger.info("agent {} params refreshed: {}", agent.name, merged)

    if updated:
        logger.info("refreshed strategy params for {} agent(s)", updated)


async def run_daily_rollover() -> dict:
    snapshots = 0
    today = date.today()

    agents = await Agent.find_all().to_list()
    for agent in agents:
        cap = float(agent.assigned_capital or 0)
        day_pnl = float(agent.current_day_pnl or 0)
        wins = int(agent.winning_trades or 0)
        total = int(agent.total_trades or 0)
        losses = max(0, total - wins)

        await AgentPerformance(
            agent_id=agent.id,
            user_id=agent.user_id,
            snapshot_date=today,
            starting_capital=cap,
            ending_capital=cap + day_pnl,
            daily_pnl=day_pnl,
            daily_pnl_pct=(day_pnl / cap * 100) if cap > 0 else 0,
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=(wins / total) if total else 0,
            strategy_params_snapshot=agent.strategy_params or {},
        ).insert()

        agent.current_day_pnl = 0
        agent.protect_mode = False
        agent.session_trade_count = 0
        await agent.save()
        snapshots += 1

    logger.info("daily rollover complete: snapshots={}", snapshots)
    return {"snapshots": snapshots, "at": datetime.now(timezone.utc).isoformat()}


# --------------------------------------------------------------------------- #
# Coach agent — runs every 2 hours
# --------------------------------------------------------------------------- #

_COACH_MIN_TRADES = 3          # don't review unless the agent has at least this many closed trades
_COACH_REVIEW_LOOKBACK = 30    # number of recent closed trades to analyse


async def run_coach_review() -> dict:
    """Periodic performance review for all active agents.

    For each agent that has >= _COACH_MIN_TRADES closed trades since the last
    review (or ever, for the first run):
      1. Compute win rate, profit factor, drawdown, and regime breakdown.
      2. Save the metrics as agent.performance_snapshot so the UI can display them.
      3. If the metrics look poor (profit_factor < 1.2 or win_rate < 40%),
         ask the Coach AI to suggest a targeted parameter nudge.
      4. Apply any nudge directly — no pause/resume needed.

    This runs in addition to the pause-triggered re-optimization, giving the
    agent a chance to course-correct before it reaches 6 consecutive losses.
    """
    from app.services.ai_optimizer import SEARCH_SPACES
    from app.services.performance import compute_metrics

    now = datetime.now(timezone.utc)
    reviewed = 0
    nudged = 0

    agents = await Agent.find({"status": {"$in": ["active", "paused"]}}).to_list()
    for agent in agents:
        try:
            strategy_type = (agent.strategy.type if agent.strategy else None)
            if not strategy_type:
                continue

            # All recent closed trades — used for snapshot (always runs)
            all_recent = (
                await Trade.find(Trade.agent_id == agent.id, Trade.status == "filled")
                .sort(-Trade.closed_at)
                .limit(_COACH_REVIEW_LOOKBACK)
                .to_list()
            )

            if not all_recent:
                continue

            metrics = compute_metrics(all_recent)
            agent.performance_snapshot = {**metrics, "computed_at": now.isoformat()}
            agent.last_coach_review_at = now
            reviewed += 1

            # New trades since last review — only used to decide whether to nudge
            last_review = getattr(agent, "last_coach_review_at", None)
            new_trade_filters: list = [Trade.agent_id == agent.id, Trade.status == "filled"]
            if last_review:
                if last_review.tzinfo is None:
                    last_review = last_review.replace(tzinfo=timezone.utc)
                new_trade_filters.append(Trade.closed_at >= last_review)
            new_trades = (
                await Trade.find(*new_trade_filters)
                .sort(-Trade.closed_at)
                .limit(_COACH_REVIEW_LOOKBACK)
                .to_list()
            )
            recent_trades = new_trades if len(new_trades) >= _COACH_MIN_TRADES else all_recent

            logger.info(
                "coach review agent {} strategy={} trades={} win_rate={:.1f}% pf={:.3f} max_dd={:.4f}",
                agent.id, strategy_type,
                metrics["total_trades"], metrics["win_rate"],
                metrics["profit_factor"], metrics["max_drawdown_usdt"],
            )

            # Log regime breakdown
            for regime, stats in (metrics.get("by_regime") or {}).items():
                logger.info(
                    "  regime={} trades={} win_rate={:.1f}% pnl={:+.4f}",
                    regime, stats["total"], stats["win_rate"], stats["pnl"],
                )

            # Only nudge if performance is poor
            profit_factor = metrics["profit_factor"]
            win_rate = metrics["win_rate"]
            needs_nudge = profit_factor < 1.2 or win_rate < 40.0

            if needs_nudge and strategy_type in SEARCH_SPACES:
                search_space = SEARCH_SPACES[strategy_type]
                strat = get_strategy(strategy_type)
                current_params = strat.merge_params(agent.strategy_params or {})

                trades_for_ai = [
                    {
                        "symbol": t.symbol,
                        "side": t.side,
                        "pnl": round(t.pnl, 4),
                        "pnl_pct": round(t.pnl_pct, 4),
                        "market_regime": t.market_regime,
                        "confidence": (t.signal_data or {}).get("confidence"),
                        "reason": (t.signal_data or {}).get("reason", "")[:80],
                    }
                    for t in recent_trades[:10]
                ]

                nudge = await grok_analyst.coach_review(
                    metrics,
                    trades_for_ai,
                    strategy_type=strategy_type,
                    current_params=current_params,
                    search_space=search_space,
                )

                if nudge:
                    # Snapshot params before change so guardian can revert if it hurts
                    _guardian_snapshot(agent)
                    merged = {**(agent.strategy_params or {}), **nudge}
                    agent.strategy_params = merged
                    nudged += 1
                    logger.info(
                        "coach nudge agent {} pf={:.3f} win_rate={:.1f}% → applying: {}",
                        agent.id, profit_factor, win_rate, nudge,
                    )
                else:
                    logger.info(
                        "coach review agent {} — poor metrics but AI suggested no change",
                        agent.id,
                    )

            await agent.save()

        except Exception as exc:
            logger.warning("coach review failed for agent {}: {}", agent.id, exc)

    logger.info("coach review complete: reviewed={} nudged={}", reviewed, nudged)
    return {"reviewed": reviewed, "nudged": nudged, "at": now.isoformat()}


# --------------------------------------------------------------------------- #
# Funding fee sync — runs every 8 hours
# --------------------------------------------------------------------------- #

async def sync_funding_fees() -> dict:
    """Fetch funding fee history from Bitget and apply to agent P&L counters.

    Funding fees on perpetual futures are charged every 8 hours and are NOT
    reflected in trade P&L — they silently drain the balance. This job pulls
    the history since the last sync and subtracts from total_pnl / day_pnl.
    """
    synced = 0
    total_applied = 0.0
    now = datetime.now(timezone.utc)

    try:
        agents = await Agent.find(Agent.status.in_(["active", "paused"])).to_list()
    except Exception as exc:
        logger.exception("funding sync: failed to query agents: {}", exc)
        return {"synced": 0, "total_funding_applied": 0.0, "error": str(exc)}

    for agent in agents:
        try:
            if not agent.api_key_id or agent.is_paper_trade:
                continue
            api_key = await ApiKey.get(agent.api_key_id)
            if not api_key:
                continue

            client = build_client(api_key)
            try:
                last_sync = agent.last_funding_sync_at
                start_ms = None
                if last_sync:
                    if last_sync.tzinfo is None:
                        last_sync = last_sync.replace(tzinfo=timezone.utc)
                    start_ms = int(last_sync.timestamp() * 1000)

                fees = await client.get_funding_fees(start_ms=start_ms, limit=100)
                if not fees:
                    agent.last_funding_sync_at = now
                    await agent.save()
                    continue

                total_fee = sum(f["amount"] for f in fees)
                if total_fee == 0:
                    agent.last_funding_sync_at = now
                    await agent.save()
                    continue

                agent.total_funding_fees = round(float(agent.total_funding_fees or 0) + total_fee, 6)
                agent.total_pnl = round(float(agent.total_pnl or 0) + total_fee, 6)
                agent.current_day_pnl = round(float(agent.current_day_pnl or 0) + total_fee, 6)
                agent.current_week_pnl = round(float(agent.current_week_pnl or 0) + total_fee, 6)
                agent.last_funding_sync_at = now
                await agent.save()

                synced += 1
                total_applied += total_fee
                logger.info(
                    "funding sync agent {}: {} fee records, total={:+.6f} USDT",
                    agent.id, len(fees), total_fee,
                )
            finally:
                await client.close()

        except Exception as exc:
            logger.warning("funding sync failed for agent {}: {}", agent.id, exc)

    logger.info("funding sync complete: agents={} total_applied={:+.6f}", synced, total_applied)
    return {"synced": synced, "total_funding_applied": round(total_applied, 6)}


# Re-export the autonomous bots so scheduler.py can import from one place.
# The actual implementations live in their dedicated modules.
__all__ = [
    "run_trading_tick_for_all_agents",
    "run_optimization_sweep",
    "reconcile_exchange_positions",
    "run_daily_rollover",
    "run_coach_review",
    "check_missed_rollover",
    "keep_alive_ping",
    "sync_funding_fees",
    "run_pnl_watchdog",
    "run_strategy_guardian",
    "run_news_sentinel",
]

