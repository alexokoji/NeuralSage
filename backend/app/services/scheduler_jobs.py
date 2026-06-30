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


async def run_trading_tick_for_all_agents() -> dict:
    """Iterate every active agent and run one trading tick."""
    processed = 0
    skipped = 0
    failed = 0

    logger.debug("trading tick starting")

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

                # Query exchange for currently open positions (best-effort)
                try:
                    exchange_positions = await client.get_positions() or []
                except Exception as exc:
                    logger.debug("agent {} exchange position query failed: {}", agent.id, exc)
                    exchange_positions = []

                exchange_symbols = {
                    str(item.get("symbol") or "").upper()
                    for item in exchange_positions
                    if item and str(item.get("symbol") or "").strip()
                }

                for pos in positions:
                    if str(pos.symbol).upper() not in exchange_symbols:
                        # DB says open but exchange says none; close locally.
                        pos.is_open = False
                        pos.current_price = pos.current_price or pos.entry_price
                        pos.updated_at = datetime.now(timezone.utc)
                        await pos.save()

                        if pos.trade_id:
                            try:
                                trade = await Trade.find_one(Trade.id == pos.trade_id)
                                if trade and trade.status == "open":
                                    trade.status = "filled"
                                    trade.closed_at = datetime.now(timezone.utc)
                                    trade.notes = f"{trade.notes or ''} reconciled as closed by exchange".strip()
                                    await trade.save()
                            except Exception:
                                pass
                        repaired += 1
                        logger.info(
                            "agent {} reconciled stale position {} ({}) to closed",
                            agent.id,
                            pos.symbol,
                            pos.id,
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
        Agent.status.in_(["active", "paused"]),
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
