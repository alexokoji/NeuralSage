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

    The pause is a safety brake after 6 consecutive losses.  Once the cooldown
    elapses the agent resumes in recovery_mode (half position size) rather than
    requiring a manual click — this keeps trading flowing while still halving
    risk until a win clears recovery mode.
    """
    from datetime import datetime, timezone, timedelta
    try:
        paused = await Agent.find({"status": "paused"}).to_list()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_PAUSE_COOLDOWN_MINUTES)
        for agent in paused:
            # Use last_tick_at as a proxy for when the agent was last active;
            # fall back to updated_at if available.
            last_active = getattr(agent, "last_tick_at", None) or getattr(agent, "updated_at", None)
            if last_active and last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            if last_active is None or last_active > cutoff:
                continue  # paused too recently — wait longer
            agent.status = "active"
            agent.recovery_mode = True  # half-size trades until first win
            await agent.save()
            logger.info(
                "agent {} auto-resumed after {}min cooldown (recovery_mode=True)",
                agent.id, _PAUSE_COOLDOWN_MINUTES,
            )
    except Exception as exc:
        logger.warning("auto-resume check failed: {}", exc)


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
    """Ping external health endpoint to prevent Render free tier spin-down.

    Render only counts external HTTP requests for its idle timer, so we
    hit our own public URL. Falls back to localhost if no external URL.
    """
    import httpx
    import os
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if external_url:
        url = f"{external_url}/health"
    else:
        url = f"http://localhost:{settings.APP_PORT}/health"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        logger.debug("keep-alive ping {} -> {}", url, resp.status_code)
    except Exception as exc:
        logger.debug("keep-alive ping failed: {}", exc)


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
            result = await optimize_strategy_async(
                strat,
                df,
                base,
                symbol=symbol,
                timeframe=a.timeframe,
                warm_starts=warm_starts,
                n_calls=20,
            )

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
