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
        await agent.save()
        snapshots += 1

    logger.info("daily rollover complete: snapshots={}", snapshots)
    return {"snapshots": snapshots, "at": datetime.now(timezone.utc).isoformat()}
