"""Real-time position stream manager.

Maintains one Bitget private WebSocket per unique live API key.
When Bitget pushes an order-fill event (SL/TP hit, liquidation, manual
close), this service resolves the matching DB position and calls the
same close logic used for paper trades — updating PnL, agent counters,
and the fleet learning system immediately.

Started/stopped from FastAPI's lifespan (app/main.py).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

# key_id (str) -> BitgetPrivateStream
_streams: dict[str, "BitgetPrivateStream"] = {}  # type: ignore[name-defined]


# ── Public API ────────────────────────────────────────────────────────────────

async def register_api_key(api_key_row) -> None:
    """Start a real-time stream for this API key if not already running.

    Safe to call multiple times for the same key — idempotent.
    Only starts streams for live Bitget keys.
    """
    from app.services.exchange.bitget_ws import BitgetPrivateStream
    from app.core.encryption import decrypt_packed

    key_id = str(api_key_row.id)
    exchange = str(api_key_row.exchange or "").lower()

    if exchange not in ("bitget",):
        return  # only Bitget has a private WS stream currently
    if api_key_row.is_testnet:
        return  # no real-time stream for testnet
    if key_id in _streams:
        return  # already running

    try:
        aad = str(api_key_row.user_id).encode()
        plaintext_key = decrypt_packed(api_key_row.encrypted_api_key, associated_data=aad)
        plaintext_secret = decrypt_packed(api_key_row.encrypted_api_secret, associated_data=aad)

        # Split secret\npassphrase (same convention as BitgetClient)
        if "\n" in plaintext_secret:
            secret, passphrase = plaintext_secret.split("\n", 1)
        else:
            from app.config import settings
            secret = plaintext_secret
            passphrase = getattr(settings, "BITGET_PASSPHRASE", "") or ""

        stream = BitgetPrivateStream(
            api_key=plaintext_key,
            api_secret=secret,
            passphrase=passphrase,
            on_order_fill=_make_fill_handler(key_id),
            key_id=key_id,
        )
        stream.start()
        _streams[key_id] = stream
        logger.info("position_stream: started real-time stream for api_key {}", key_id)
    except Exception as exc:
        logger.warning("position_stream: failed to start stream for {}: {}", key_id, exc)


async def unregister_api_key(key_id: str) -> None:
    stream = _streams.pop(str(key_id), None)
    if stream:
        await stream.stop()
        logger.info("position_stream: stopped stream for api_key {}", key_id)


async def start_all_active() -> None:
    """Called at app startup — boot streams for all active live agents."""
    from app.models.agent import Agent
    from app.models.api_key import ApiKey

    try:
        agents = await Agent.find(Agent.status == "active").to_list()
        seen_keys: set[str] = set()
        for agent in agents:
            if not agent.api_key_id or str(agent.api_key_id) in seen_keys:
                continue
            api_key = await ApiKey.get(agent.api_key_id)
            if api_key and not agent.is_paper_trade:
                await register_api_key(api_key)
                seen_keys.add(str(agent.api_key_id))
        logger.info("position_stream: {} stream(s) started at startup", len(_streams))
    except Exception as exc:
        logger.warning("position_stream: startup failed: {}", exc)


async def stop_all() -> None:
    """Called at app shutdown."""
    tasks = [stream.stop() for stream in list(_streams.values())]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _streams.clear()
    logger.info("position_stream: all streams stopped")


# ── Fill handler ──────────────────────────────────────────────────────────────

def _make_fill_handler(api_key_id: str):
    """Return an async callback bound to this API key."""

    async def on_fill(fill: dict) -> None:
        await _handle_fill(api_key_id, fill)

    return on_fill


async def _handle_fill(api_key_id: str, fill: dict) -> None:
    """Process an order-fill event pushed by Bitget.

    Finds the matching open DB position, computes actual PnL using the
    real exchange fill price, closes the position, and updates everything
    — agent stats, learning system, notifications.
    """
    from app.models.agent import Agent
    from app.models.api_key import ApiKey
    from app.models.position import Position
    from app.models.trade import Trade
    from app.services.notifications import NotificationService

    symbol = fill["symbol"]
    avg_price = float(fill["avg_fill_price"] or 0)
    exchange_pnl = float(fill["pnl"] or 0)

    if avg_price <= 0:
        logger.debug("position_stream: ignoring fill with no price: {}", fill)
        return

    # Only care about closing fills (reduce_only) or any fill that matches an open position
    try:
        api_key = await ApiKey.get(api_key_id)
        if not api_key:
            return

        # Find open positions on this symbol belonging to any agent using this key
        agents = await Agent.find(Agent.api_key_id == api_key.id).to_list()
        for agent in agents:
            positions = await Position.find(
                Position.agent_id == agent.id,
                Position.symbol == symbol,
                Position.is_open == True,  # noqa: E712
            ).to_list()

            for pos in positions:
                entry_price = float(pos.entry_price)
                qty = float(pos.quantity or 0)

                # Use exchange-reported PnL when available (includes fees).
                if exchange_pnl != 0:
                    gross = exchange_pnl
                else:
                    gross = (avg_price - entry_price) * qty
                    if pos.side == "short":
                        gross = -gross

                # Persist position closure.
                pos.is_open = False
                pos.current_price = avg_price
                pos.unrealized_pnl = gross
                pos.updated_at = datetime.now(timezone.utc)
                await pos.save()

                # Update the linked trade doc.
                if pos.trade_id:
                    try:
                        trade = await Trade.find_one(Trade.id == pos.trade_id)
                        if trade and trade.status == "open":
                            trade.exit_price = avg_price
                            trade.pnl = gross
                            trade.pnl_pct = (gross / max(entry_price * qty, 1e-9)) * 100
                            trade.status = "filled"
                            trade.closed_at = datetime.now(timezone.utc)
                            trade.notes = (
                                f"closed by exchange via WebSocket "
                                f"(fill={fill['order_id']} price_source=exchange_fill)"
                            )
                            await trade.save()
                    except Exception as exc:
                        logger.debug("position_stream: trade update failed: {}", exc)

                # Update agent P&L counters.
                agent.total_pnl = float(agent.total_pnl or 0) + gross
                agent.current_day_pnl = float(agent.current_day_pnl or 0) + gross
                agent.current_week_pnl = float(agent.current_week_pnl or 0) + gross
                if gross > 0:
                    agent.winning_trades = (agent.winning_trades or 0) + 1
                await agent.save()

                # Feed the learning system.
                try:
                    from app.services.learning import LearningService
                    strategy_type = agent.strategy.type if agent.strategy else None
                    if strategy_type:
                        await LearningService.record_trade_outcome(
                            agent_id=agent.id,
                            strategy_type=strategy_type,
                            symbol=symbol,
                            timeframe=agent.timeframe,
                            pnl=gross,
                        )
                except Exception:
                    pass

                await NotificationService.create(
                    user_id=agent.user_id,
                    type="trade_closed",
                    title=f"{agent.name} closed {pos.side} {symbol}",
                    message=f"PnL {gross:+.4f} (real-time fill @ {avg_price})",
                    data={"agent_id": str(agent.id), "trade_id": str(pos.trade_id)},
                )

                logger.info(
                    "position_stream: agent {} {} {} closed via WS fill:"
                    " pnl={:.4f} fill_price={}",
                    agent.id, symbol, pos.side, gross, avg_price,
                )

    except Exception as exc:
        logger.exception("position_stream: _handle_fill error for {}: {}", symbol, exc)
