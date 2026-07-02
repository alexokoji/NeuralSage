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
        # Auto-normalize tiny accounts: apply tighter stop-loss defaults and
        # relax max_risk_per_trade so the RiskEngine can size a non-zero qty.
        try:
            cap = float(agent.assigned_capital or 0)
            if cap > 0 and cap <= self._SMALL_ACCOUNT_CAP_THRESHOLD:
                sp = agent.strategy_params or {}
                cur_sl = float(sp.get("stop_loss_pct") or 0)
                applied = False
                # For tiny accounts the SL must be wide enough that the qty
                # calculation produces a result above the exchange minimum.
                # A very tight SL (e.g. 0.10%) forces a huge qty (big notional,
                # high leverage) relative to the balance.  Clamp to 0.5% so the
                # position stays within a manageable leverage band.
                if cur_sl == 0 or cur_sl < self._SMALL_ACCOUNT_DEFAULT_SL:
                    sp["stop_loss_pct"] = float(self._SMALL_ACCOUNT_DEFAULT_SL)
                    agent.strategy_params = sp
                    applied = True
                # Ensure max_risk_per_trade is at least the tiny-account default.
                # Note: RiskEngine.cap_risk_per_trade also allows 5% for capital <= $50.
                if (agent.max_risk_per_trade or 0) < self._SMALL_ACCOUNT_DEFAULT_MAX_RISK_PCT:
                    agent.max_risk_per_trade = float(self._SMALL_ACCOUNT_DEFAULT_MAX_RISK_PCT)
                    applied = True
                if applied:
                    logger.info(
                        "agent {} small-account normalization: capital=${:.2f},"
                        " stop_loss_pct={:.2f}%, max_risk_per_trade={:.2f}%",
                        agent.id,
                        cap,
                        sp.get("stop_loss_pct", cur_sl),
                        agent.max_risk_per_trade,
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

        # --- Daily profit protection (resets every day via rollover) ---
        capital = float(agent.assigned_capital or 0)
        if capital > 0:
            day_pnl_pct = (float(agent.current_day_pnl or 0) / capital) * 100
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

        # --- Two-phase scan: screener filters, AI analyses top candidates ---
        # Phase 1: Run screener on ALL symbols (free, no API calls)
        candidates: list[tuple[str, Any, Any, Any]] = []  # (symbol, df, screener_signal, open_pos)
        signals_summary: list[str] = []
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

                # Auto TP/SL for paper trades
                if open_position is not None:
                    open_position.current_price = last_price
                    sl = float(open_position.stop_loss or 0)
                    tp = float(open_position.take_profit or 0)
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

                # Always include: non-hold signals, open positions
                # Conditionally include: holds with high confidence (near threshold)
                if (
                    screener_signal.action != "hold"
                    or open_position is not None
                    or screener_signal.confidence > 0.42
                ):
                    candidates.append((symbol, df, screener_signal, open_position))

            # Phase 2: Groq analyses the SINGLE best candidate per agent
            # Phase 3: GPT makes final decision (1 call per agent)
            # Total: 3 agents × 1 Groq + 1 GPT = 6 AI calls per tick
            candidates.sort(key=lambda c: (c[2].action == "hold", -c[2].confidence))

            # Allow agents to prefer the raw screener if its confidence exceeds
            # the AI's confidence by a small margin (configurable per-agent).
            # groq_best stores: (symbol, df, screener_signal, groq_signal, open_pos)
            groq_best = None  # (symbol, df, screener_signal, groq_signal, open_pos)
            screener_advantage_delta = float(
                (agent.strategy_params or {}).get("screener_advantage_delta", 0.10)
            )
            for symbol, df, screener_signal, open_position in candidates[:1]:
                try:
                    groq_signal = await grok_analyst.groq_analyse(
                        screener_signal, df,
                        symbol=symbol, timeframe=agent.timeframe,
                        strategy_type=strategy.type,
                        strategy_params=agent.strategy_params or {},
                        agent_context={
                            "agent_name": agent.name,
                            "total_trades": agent.total_trades,
                            "winning_trades": agent.winning_trades,
                            "total_pnl": float(agent.total_pnl or 0),
                            "current_day_pnl": float(agent.current_day_pnl or 0),
                            "in_position": open_position is not None,
                            "screener_said": screener_signal.action,
                            "screener_confidence": screener_signal.confidence,
                        },
                    )
                    ai_used = True
                    if groq_signal.action in ("enter_long", "enter_short", "exit"):
                        if groq_best is None or groq_signal.confidence > (groq_best[3].confidence if groq_best[3] else 0):
                            groq_best = (symbol, df, screener_signal, groq_signal, open_position)
                except Exception as exc:
                    logger.debug("agent {} {} Groq analysis failed: {}", agent.id, symbol, exc)

            # Phase 3: GPT final decision on Groq's best pick
            if groq_best:
                symbol, df, screener_signal, groq_signal, open_position = groq_best
                try:
                    ai_used = True
                    final_signal = await grok_analyst.gpt_decide(
                        groq_signal, df,
                        symbol=symbol, timeframe=agent.timeframe,
                        agent_name=agent.name,
                    )
                    agent.last_signal = final_signal.action
                    agent.last_signal_symbol = symbol
                    await self._execute_signal(
                        agent,
                        api_key,
                        client,
                        symbol,
                        df,
                        final_signal,
                        open_position,
                        ai_available=True,
                    )
                    if final_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{final_signal.action}")
                except Exception as exc:
                    logger.warning("agent {} GPT decision failed: {} — using Groq signal", agent.id, exc)
                    agent.last_signal = groq_signal.action
                    agent.last_signal_symbol = symbol
                    await self._execute_signal(
                        agent,
                        api_key,
                        client,
                        symbol,
                        df,
                        groq_signal,
                        open_position,
                        ai_available=True,
                    )
                    if groq_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{groq_signal.action}")
            elif candidates:
                # AI unavailable — execute the best screener signal directly
                best = candidates[0]
                symbol, df, screener_signal, open_position = best
                if screener_signal.action in ("enter_long", "enter_short") and screener_signal.confidence >= 0.50:
                    logger.info("agent {} AI unavailable — executing screener fallback {} {} (conf {:.2f})",
                                agent.id, symbol, screener_signal.action, screener_signal.confidence)
                    agent.last_signal = screener_signal.action
                    agent.last_signal_symbol = f"{symbol} (no AI)"
                    try:
                        await self._execute_signal(
                            agent,
                            api_key,
                            client,
                            symbol,
                            df,
                            screener_signal,
                            open_position,
                            ai_available=False,
                        )
                    except Exception as exc:
                        logger.error("agent {} _execute_signal for {} raised exception: {}", agent.id, symbol, exc, exc_info=True)
                    if screener_signal.action != "hold":
                        signals_summary.append(f"{symbol}:{screener_signal.action}")

        finally:
            await client.close()

        pairs_checked = len(agent.trading_pairs or [])
        if signals_summary:
            agent.last_error = None
            agent.last_signal_symbol = " | ".join(signals_summary[:3])
        else:
            agent.last_signal = "hold"
            agent.last_signal_symbol = f"scanned {pairs_checked} pairs"
            agent.last_error = None

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
        # Emit concise per-agent metric for observability
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
        # Reconcile live open positions so the agent can observe unrealized P&L
        try:
            await self._reconcile_open_positions(agent, api_key, client)
        except Exception as exc:
            logger.debug("agent {} reconciliation failed: {}", agent.id, exc)

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
        """Sync open positions every tick — mirrors paper trading's SL/TP cadence.

        For live agents:
          1. Ask the exchange which positions are still open.
          2. Any DB position no longer on the exchange was closed server-side
             (SL/TP triggered, liquidation, manual close).  Fetch the real fill
             price from order history and close it properly — same path as paper.
          3. For still-open positions update unrealized P&L from latest candle.
        """
        positions = await Position.find(
            Position.agent_id == agent.id,
            Position.is_open == True,  # noqa: E712
        ).to_list()
        if not positions:
            return

        # ── For live agents: detect exchange-closed positions every tick ──────
        exchange_symbols: set[str] = set()
        closed_orders_by_symbol: dict[str, list[dict]] = {}
        exchange_query_ok = False

        if not agent.is_paper_trade:
            try:
                exchange_positions = await client.get_positions() or []
                exchange_symbols = {
                    str(p.get("symbol") or "").upper()
                    for p in exchange_positions
                    if p and str(p.get("symbol") or "").strip()
                }
                exchange_query_ok = True
            except AttributeError:
                pass  # exchange doesn't support get_positions
            except Exception as exc:
                logger.debug("agent {} get_positions failed in reconcile: {}", agent.id, exc)

            if exchange_query_ok:
                # Pre-fetch last 2 h of closed orders to resolve fill prices.
                try:
                    import time as _time
                    lookback_ms = int((_time.time() - 7_200) * 1000)
                    raw_closed = await client.get_closed_orders(limit=50, start_ms=lookback_ms)
                    for o in raw_closed:
                        closed_orders_by_symbol.setdefault(o["symbol"], []).append(o)
                except AttributeError:
                    pass
                except Exception as exc:
                    logger.debug("agent {} get_closed_orders failed: {}", agent.id, exc)

        for pos in positions:
            # ── Detect exchange-side closure (live trades only) ───────────────
            if not agent.is_paper_trade and exchange_query_ok:
                if str(pos.symbol).upper() not in exchange_symbols:
                    await self._close_position_from_exchange(
                        agent, api_key, pos, closed_orders_by_symbol
                    )
                    continue  # position is now closed — skip price update

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
                gross = (current - entry) * qty
                if pos.side == "short":
                    gross = -gross

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
                actual_fill = max(qty_matches, key=lambda o: o["closed_at_ms"])

        if actual_fill and actual_fill["avg_fill_price"] > 0:
            exit_price = actual_fill["avg_fill_price"]
            gross = actual_fill["pnl"] if actual_fill["pnl"] != 0 else (
                (exit_price - entry_price) * qty * (1 if pos.side == "long" else -1)
            )
            price_source = "exchange_fill"
        else:
            exit_price = float(pos.current_price or entry_price)
            gross = (exit_price - entry_price) * qty
            if pos.side == "short":
                gross = -gross
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

            # Use strategy defaults for SL/TP, not AI suggestions
            # AI can suggest tighter values but never looser than strategy defaults
            strat_params = agent.strategy_params or {}
            default_sl = float(strat_params.get("stop_loss_pct", 1.0))
            default_tp = float(strat_params.get("take_profit_pct",
                              strat_params.get("profit_target_pct", 2.5)))

            ai_sl = signal.suggested_stop_loss_pct or default_sl
            ai_tp = signal.suggested_take_profit_pct or default_tp

            # SL: use the TIGHTER of strategy default and AI suggestion
            sl_pct = min(ai_sl, default_sl)
            # TP: use the LARGER of strategy default and AI suggestion
            tp_pct = max(ai_tp, default_tp)

            # Enforce minimum 1.5:1 reward-to-risk ratio
            if tp_pct < sl_pct * 1.5:
                tp_pct = round(sl_pct * 1.5, 3)

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
            )
            logger.debug("agent {} {} _open_trade completed", agent.id, symbol)
        positions = await Position.find(
            Position.agent_id == agent.id,
            Position.is_open == True,  # noqa: E712
        ).to_list()

        if not positions:
            return 0

        # Fetch fresh prices for accurate P&L
        for pos in positions:
            try:
                raw = await client.get_candles(pos.symbol, agent.timeframe or "5m", limit=2)
                if raw:
                    from app.services.strategy.indicators import candles_to_df
                    df = candles_to_df(raw)
                    pos.current_price = float(df["close"].iloc[-1])
            except Exception:
                pass

        reason = "position management"
        closed = 0
        tightened = 0
        for pos in positions:
            entry = float(pos.entry_price)
            current = float(pos.current_price or entry)
            sl = float(pos.stop_loss or entry)

            # Calculate unrealized P&L %
            if pos.side == "long":
                pnl_pct = (current - entry) / entry * 100
                distance_to_sl = (entry - sl) / entry * 100 if sl < entry else 0
                at_risk_pct = (entry - current) / entry * 100 if current < entry else 0
            else:
                pnl_pct = (entry - current) / entry * 100
                distance_to_sl = (sl - entry) / entry * 100 if sl > entry else 0
                at_risk_pct = (current - entry) / entry * 100 if current > entry else 0

            try:
                if pnl_pct > 0.05:
                    # In profit → close to lock in gains
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (locking profit +{pnl_pct:.2f}%)")
                    closed += 1
                elif abs(pnl_pct) <= 0.1:
                    # Breakeven → close to free capital
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (breakeven)")
                    closed += 1
                elif distance_to_sl > 0 and at_risk_pct > distance_to_sl * 0.6:
                    # Losing and close to stop loss → close before it gets worse
                    await self._close_position(agent, api_key, client, pos, current, f"{reason} (near SL, cutting loss)")
                    closed += 1
                else:
                    # Position still has room to run → tighten stop to breakeven
                    if pos.side == "long":
                        pos.stop_loss = max(entry, sl)
                    else:
                        pos.stop_loss = min(entry, sl) if sl > 0 else entry
                    await pos.save()
                    tightened += 1
                    logger.info(
                        "agent {} {}: tightened SL to breakeven (not closing — PnL {:.2f}%)",
                        agent.id, pos.symbol, pnl_pct,
                    )
            except Exception as exc:
                logger.warning("failed to handle position {} for agent {}: {}", pos.id, agent.id, exc)

        if closed or tightened:
            logger.info(
                "agent {}: {} position(s) closed, {} tightened to breakeven — {}",
                agent.id, closed, tightened, reason,
            )
        return closed

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
                **(signal.metadata or {}),
            },
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
            logger.info("agent {} {} paper trade path: qty={} entry={}", agent.id, symbol, quantity, entry_price)
            placed = OrderResult(
                exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
                status="filled",
                avg_fill_price=entry_price,
                filled_qty=quantity,
                raw={"paper": True},
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
        order = OrderRequest(
            symbol=position.symbol,
            side="sell" if position.side == "long" else "buy",
            order_type="market",
            quantity=float(position.quantity),
            reduce_only=True,
            client_order_id=f"agent-{agent.id}-close-{uuid.uuid4().hex[:8]}",
        )
        close_failed = False
        if not agent.is_paper_trade:
            try:
                await client.place_order(order)
            except ExchangeError as exc:
                close_failed = True
                agent.last_error = f"close failed: {exc}"
                await RiskEngine.log_risk_event(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    event_type="api_error",
                    severity="critical",
                    message=f"position close failed: {exc}",
                    details={"symbol": position.symbol},
                )
                logger.error("agent {} {} close order failed (will persist closure locally): {}", agent.id, position.symbol, exc)
            except Exception as exc:
                close_failed = True
                agent.last_error = f"close failed: {exc}"
                logger.exception("agent {} {} unexpected error during close (will persist closure locally): {}", agent.id, position.symbol, exc)

        entry_price = float(position.entry_price)
        qty = float(position.quantity)
        gross = (last_price - entry_price) * qty
        if position.side == "short":
            gross = -gross

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
            # If the exchange close failed, still persist the closure locally
            # so the agent doesn't get stuck with stale open positions.
            note = reason
            if close_failed:
                note = f"{reason} (exchange close failed — persisted locally)"
            trade.notes = note
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

        # After 3 consecutive losses, AI re-optimizes params (agent keeps trading)
        if gross < 0:
            loss_streak = 0
            streak_trades = await Trade.find(
                Trade.agent_id == agent.id, Trade.status == "filled", Trade.closed_at != None,
            ).sort(-Trade.closed_at).limit(5).to_list()
            for t in streak_trades:
                if float(t.pnl or 0) < 0:
                    loss_streak += 1
                else:
                    break

            if loss_streak >= 3:
                try:
                    await self._emergency_optimize(agent, position.symbol, loss_streak)
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

        # Apply tighter risk overrides on top of optimized params
        new_params = dict(result.best_params)
        if "stop_loss_pct" in new_params:
            new_params["stop_loss_pct"] = min(new_params["stop_loss_pct"], 0.8)
        if "min_confidence" in new_params:
            new_params["min_confidence"] = max(new_params["min_confidence"], 0.7)
        if "position_size_pct" in new_params:
            new_params["position_size_pct"] = min(new_params["position_size_pct"], 1.5)

        agent.strategy_params = new_params
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
