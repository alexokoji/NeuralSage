"""P&L Watchdog — Bot 1.

Runs every 15 minutes for every active live (non-paper) agent.

The watchdog detects and corrects drift between the DB-recorded P&L and
the exchange's actual closed-order fills. Common causes of drift:

  * SL/TP filled at a price that differs from our candle-price estimate.
  * Exchange fees applied differently than our 0.12% round-trip estimate.
  * Funding rates (handled separately by sync_funding_fees, but residual
    drift can still accumulate between 8-hour syncs).

Algorithm per agent:
  1. Find all DB trades closed since the last watchdog pass that were NOT
     sourced from an exchange fill ("exchange_fill" not in notes).
  2. Fetch recent closed orders from the exchange for matching symbols.
  3. Match by (symbol, qty). If exchange PnL differs from DB by > $0.001,
     update the DB trade and accumulate the correction.
  4. Apply the net correction to agent counters and store pnl_drift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.trade import Trade
from app.services.exchange import build_client
from app.services.exchange.base import ExchangeError


async def run_pnl_watchdog() -> None:
    """Entry point called by the scheduler every 15 minutes."""
    try:
        agents = await Agent.find(
            {"status": {"$in": ["active", "paused"]}, "is_paper_trade": {"$ne": True}},
        ).to_list()
        logger.info("pnl_watchdog: checking {} live agents", len(agents))
        for agent in agents:
            try:
                await _watchdog_agent(agent)
            except Exception as exc:
                logger.warning("pnl_watchdog: agent {} failed: {}", agent.id, exc)
    except Exception as exc:
        logger.error("pnl_watchdog run failed: {}", exc)


async def _watchdog_agent(agent: Agent) -> None:
    if not agent.api_key_id:
        return

    api_key = await ApiKey.get(agent.api_key_id)
    if not api_key:
        return

    now = datetime.now(timezone.utc)
    since = getattr(agent, "last_pnl_watchdog_at", None)
    if since is None:
        since = now - timedelta(hours=24)
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    # Trades closed in this window, not yet matched to exchange fills
    window_trades = await Trade.find(
        Trade.agent_id == agent.id,
        Trade.status == "filled",
        {"closed_at": {"$gte": since}},
    ).to_list()

    if not window_trades:
        agent.last_pnl_watchdog_at = now
        await agent.save()
        return

    client = build_client(api_key)
    exchange_orders: list[dict] = []
    try:
        symbols = list({t.symbol for t in window_trades})
        start_ms = int(since.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        for sym in symbols[:8]:  # cap to avoid rate limits
            try:
                orders = await client.get_closed_orders(
                    symbol=sym, limit=50, start_ms=start_ms,
                )
                exchange_orders.extend(orders)
            except (AttributeError, ExchangeError, Exception) as exc:
                logger.debug("pnl_watchdog: closed orders for {} failed: {}", sym, exc)
    finally:
        await client.close()

    if not exchange_orders:
        agent.last_pnl_watchdog_at = now
        agent.pnl_drift = 0.0
        await agent.save()
        return

    # Build lookup: (SYMBOL:rounded_qty) → exchange order
    ex_lookup: dict[str, dict] = {}
    for o in exchange_orders:
        key = f"{str(o.get('symbol', '')).upper()}:{round(float(o.get('filled_qty', 0) or 0), 4)}"
        # Keep only closing-side orders (trade_side == "close") when available
        existing = ex_lookup.get(key)
        if existing is None or o.get("trade_side") == "close":
            ex_lookup[key] = o

    total_correction = 0.0
    for trade in window_trades:
        notes = trade.notes or ""
        if "exchange_fill" in notes or "corrected by watchdog" in notes:
            continue  # already accurate

        qty = float(trade.quantity or 0)
        key = f"{str(trade.symbol).upper()}:{round(qty, 4)}"
        ex_order = ex_lookup.get(key)
        if not ex_order:
            continue

        ex_pnl = float(ex_order.get("pnl") or 0)
        if ex_pnl == 0:
            continue  # exchange didn't report PnL for this order

        db_pnl = float(trade.pnl or 0)
        correction = ex_pnl - db_pnl

        if abs(correction) < 0.001:
            continue

        logger.info(
            "pnl_watchdog: trade {} {} db={:.4f} exchange={:.4f} correction={:+.4f}",
            trade.id, trade.symbol, db_pnl, ex_pnl, correction,
        )
        trade.pnl = round(ex_pnl, 6)
        trade.notes = f"{notes} | corrected by watchdog ({correction:+.4f})"
        await trade.save()
        total_correction += correction

    if abs(total_correction) > 0.001:
        agent.total_pnl = round(float(agent.total_pnl or 0) + total_correction, 6)
        agent.current_day_pnl = round(float(agent.current_day_pnl or 0) + total_correction, 6)
        agent.current_week_pnl = round(float(agent.current_week_pnl or 0) + total_correction, 6)
        agent.pnl_drift = round(total_correction, 6)
        logger.info(
            "pnl_watchdog: agent {} corrected {:+.4f} USDT across {} trades",
            agent.id, total_correction, len(window_trades),
        )
    else:
        agent.pnl_drift = 0.0

    agent.last_pnl_watchdog_at = now
    await agent.save()
