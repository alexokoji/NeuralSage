"""Composite strategy — user-defined rules built from indicator conditions.

A composite strategy is a JSON-defined set of entry/exit rules. Each rule
checks an indicator against a threshold. All entry conditions must be true
(AND logic) for a signal to fire.

Example rule set:
{
  "entry_long": [
    {"indicator": "rsi", "period": 14, "op": "<", "value": 35},
    {"indicator": "ema_cross", "fast": 9, "slow": 21, "direction": "bullish"}
  ],
  "entry_short": [
    {"indicator": "rsi", "period": 14, "op": ">", "value": 65},
    {"indicator": "ema_cross", "fast": 9, "slow": 21, "direction": "bearish"}
  ],
  "exit_long": [
    {"indicator": "rsi", "period": 14, "op": ">", "value": 60}
  ],
  "exit_short": [
    {"indicator": "rsi", "period": 14, "op": "<", "value": 40}
  ]
}
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.strategy.base import Signal, Strategy, StrategyContext
from app.services.strategy.indicators import ema, rsi, atr


class CompositeStrategy(Strategy):
    type = "composite"
    default_params: dict[str, Any] = {
        "rules": {},
        "stop_loss_pct": 1.0,
        "take_profit_pct": 2.5,
        "min_confidence": 0.6,
        "position_size_pct": 2.0,
    }

    def evaluate(self, candles: pd.DataFrame, params: dict[str, Any], ctx: StrategyContext) -> Signal:
        params = self.merge_params(params)
        rules = params.get("rules", {})
        if not rules:
            return Signal("hold", 0.0, "no rules defined")

        if len(candles) < 50:
            return Signal("hold", 0.0, "insufficient candles")

        indicators = self._compute_indicators(candles, rules)

        meta = {k: float(v) if isinstance(v, (int, float)) else v for k, v in indicators.items()}

        if ctx.in_position:
            exit_key = f"exit_{ctx.position_side}"
            exit_rules = rules.get(exit_key, [])
            if exit_rules and self._all_conditions_met(exit_rules, indicators):
                return Signal("exit", 0.7, f"composite exit ({exit_key})", metadata=meta)
            return Signal("hold", 0.5, "composite: in position, no exit trigger", metadata=meta)

        # Check entry conditions
        long_rules = rules.get("entry_long", [])
        short_rules = rules.get("entry_short", [])

        long_match = bool(long_rules) and self._all_conditions_met(long_rules, indicators)
        short_match = bool(short_rules) and self._all_conditions_met(short_rules, indicators)

        if long_match and not short_match:
            conf = self._calc_confidence(long_rules, indicators)
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_long", conf, "composite entry (long)",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )
        if short_match and not long_match:
            conf = self._calc_confidence(short_rules, indicators)
            if conf >= params["min_confidence"]:
                return Signal(
                    "enter_short", conf, "composite entry (short)",
                    suggested_stop_loss_pct=params["stop_loss_pct"],
                    suggested_take_profit_pct=params["take_profit_pct"],
                    metadata=meta,
                )

        return Signal("hold", 0.4, "composite: no conditions met", metadata=meta)

    def _compute_indicators(self, candles: pd.DataFrame, rules: dict) -> dict[str, Any]:
        close = candles["close"]
        result: dict[str, Any] = {"last_close": float(close.iloc[-1])}

        all_rules = []
        for rule_list in rules.values():
            if isinstance(rule_list, list):
                all_rules.extend(rule_list)

        indicators_needed = {r.get("indicator") for r in all_rules if isinstance(r, dict)}

        if "rsi" in indicators_needed:
            period = 14
            for r in all_rules:
                if r.get("indicator") == "rsi" and "period" in r:
                    period = int(r["period"])
            rsi_series = rsi(close, period)
            result["rsi"] = float(rsi_series.iloc[-1])
            result["rsi_prev"] = float(rsi_series.iloc[-2])

        if "ema_cross" in indicators_needed or "ema" in indicators_needed:
            for r in all_rules:
                if r.get("indicator") in ("ema_cross", "ema"):
                    fast_p = int(r.get("fast", 9))
                    slow_p = int(r.get("slow", 21))
                    fast_ema = ema(close, fast_p)
                    slow_ema = ema(close, slow_p)
                    result[f"ema_{fast_p}"] = float(fast_ema.iloc[-1])
                    result[f"ema_{slow_p}"] = float(slow_ema.iloc[-1])
                    result["ema_diff"] = float(fast_ema.iloc[-1] - slow_ema.iloc[-1])
                    result["ema_diff_prev"] = float(fast_ema.iloc[-2] - slow_ema.iloc[-2])
                    result["ema_cross_bullish"] = result["ema_diff"] > 0 and result["ema_diff_prev"] <= 0
                    result["ema_cross_bearish"] = result["ema_diff"] < 0 and result["ema_diff_prev"] >= 0

        if "atr" in indicators_needed:
            period = 14
            for r in all_rules:
                if r.get("indicator") == "atr" and "period" in r:
                    period = int(r["period"])
            result["atr"] = float(atr(candles, period).iloc[-1])

        if "volume" in indicators_needed:
            vol = candles["volume"]
            result["volume"] = float(vol.iloc[-1])
            result["volume_avg"] = float(vol.tail(20).mean())
            result["volume_ratio"] = result["volume"] / max(result["volume_avg"], 1e-9)

        if "price_change" in indicators_needed:
            result["price_change_pct"] = (float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100

        return result

    def _all_conditions_met(self, rules: list[dict], indicators: dict) -> bool:
        for rule in rules:
            if not self._check_condition(rule, indicators):
                return False
        return True

    def _check_condition(self, rule: dict, indicators: dict) -> bool:
        indicator = rule.get("indicator", "")
        op = rule.get("op", "")
        value = rule.get("value", 0)

        if indicator == "rsi":
            actual = indicators.get("rsi", 50)
            return self._compare(actual, op, value)

        if indicator == "ema_cross":
            direction = rule.get("direction", "bullish")
            if direction == "bullish":
                return indicators.get("ema_cross_bullish", False)
            return indicators.get("ema_cross_bearish", False)

        if indicator == "ema":
            fast_p = int(rule.get("fast", 9))
            actual = indicators.get(f"ema_{fast_p}", 0)
            return self._compare(actual, op, value)

        if indicator == "volume":
            actual = indicators.get("volume_ratio", 1)
            return self._compare(actual, op, value)

        if indicator == "price_change":
            actual = indicators.get("price_change_pct", 0)
            return self._compare(actual, op, value)

        if indicator == "atr":
            actual = indicators.get("atr", 0)
            return self._compare(actual, op, value)

        return False

    @staticmethod
    def _compare(actual: float, op: str, value: float) -> bool:
        if op == "<":
            return actual < value
        if op == ">":
            return actual > value
        if op == "<=":
            return actual <= value
        if op == ">=":
            return actual >= value
        if op == "==":
            return abs(actual - value) < 1e-9
        return False

    def _calc_confidence(self, rules: list[dict], indicators: dict) -> float:
        if not rules:
            return 0.5
        matched = sum(1 for r in rules if self._check_condition(r, indicators))
        return min(0.85, 0.4 + (matched / len(rules)) * 0.45)
