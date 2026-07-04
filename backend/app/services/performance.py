"""Performance metrics calculator.

Computes statistics over a list of closed Trade documents:
  - win_rate, profit_factor, avg_pnl, max_drawdown
  - regime breakdown (how each market regime — trending/ranging — performs)

Used by the coach agent to decide whether to nudge strategy params.
"""
from __future__ import annotations

from typing import Any

from app.models.trade import Trade


def compute_metrics(trades: list[Trade]) -> dict[str, Any]:
    """Return a metrics dict for a list of Trade documents.

    Only closed trades (status == "filled") are counted.
    Returns zeros / empty dicts when there are no closed trades.
    """
    closed = [
        t for t in trades
        if t.status == "filled" and t.pnl is not None
    ]
    if not closed:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_pnl": 0.0,
            "max_drawdown_usdt": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "by_regime": {},
        }

    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]

    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_profit / max(gross_loss, 1e-9)
    win_rate = len(wins) / len(closed) * 100
    avg_pnl = sum(t.pnl for t in closed) / len(closed)

    # Max drawdown: largest peak-to-trough drop in cumulative PnL
    sorted_trades = sorted(closed, key=lambda x: x.closed_at or x.created_at)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        cumulative += t.pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Per-regime breakdown — market_regime field set at trade open
    regime_stats: dict[str, dict[str, Any]] = {}
    for t in closed:
        regime = t.market_regime or (t.signal_data or {}).get("market_regime") or "unknown"
        if regime not in regime_stats:
            regime_stats[regime] = {"wins": 0, "total": 0, "pnl": 0.0}
        regime_stats[regime]["total"] += 1
        if t.pnl > 0:
            regime_stats[regime]["wins"] += 1
        regime_stats[regime]["pnl"] = round(regime_stats[regime]["pnl"] + t.pnl, 4)

    for stats in regime_stats.values():
        stats["win_rate"] = round(stats["wins"] / max(stats["total"], 1) * 100, 1)
        stats["pnl"] = round(stats["pnl"], 4)

    return {
        "total_trades": len(closed),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 3),
        "avg_pnl": round(avg_pnl, 4),
        "max_drawdown_usdt": round(max_dd, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "by_regime": regime_stats,
    }
