"""Strategy Guardian — Bot 2.

Runs every 20 minutes and acts as the system's self-awareness layer:

  1. Computes rolling performance metrics for each active agent.
  2. Sets system_mood ("danger" | "cautious" | "neutral" | "confident").
  3. Checks if the last param change improved or hurt performance.
  4. If a change hurt (3+ losses since change), reverts to the previous params.
  5. Sends a notification when mood deteriorates to danger.

The guardian is the "voice of the system" — it watches what's working and
tells the AI brain how to behave. gpt_decide reads agent.system_mood and
agent.guardian_notes to adjust its prompt tone and confidence requirements.
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from app.models.agent import Agent
from app.models.trade import Trade
from app.services.notifications import NotificationService


async def run_strategy_guardian() -> None:
    """Entry point called by the scheduler every 20 minutes."""
    try:
        agents = await Agent.find(
            {"status": {"$in": ["active", "paused"]}},
        ).to_list()
        candidates = [a for a in agents if (a.total_trades or 0) >= 3]
        logger.info("strategy_guardian: reviewing {} agents", len(candidates))
        for agent in candidates:
            try:
                await _review_agent(agent)
            except Exception as exc:
                logger.warning("strategy_guardian: agent {} error: {}", agent.id, exc)
    except Exception as exc:
        logger.error("strategy_guardian run failed: {}", exc)


async def _review_agent(agent: Agent) -> None:
    trades = await Trade.find(
        Trade.agent_id == agent.id,
        Trade.status == "filled",
    ).sort(-Trade.closed_at).limit(40).to_list()

    if not trades:
        return

    # ── Rolling metrics (last 20 trades for mood) ──────────────────────────
    recent = trades[:20]
    total = len(recent)
    wins = sum(1 for t in recent if float(t.pnl or 0) > 0)
    win_rate = (wins / total) * 100

    loss_streak = 0
    for t in recent:
        if float(t.pnl or 0) < 0:
            loss_streak += 1
        else:
            break

    gross_profit = sum(float(t.pnl or 0) for t in recent if float(t.pnl or 0) > 0)
    gross_loss = abs(sum(float(t.pnl or 0) for t in recent if float(t.pnl or 0) < 0))
    pf = gross_profit / max(gross_loss, 1e-9)
    avg_pnl = sum(float(t.pnl or 0) for t in recent) / max(total, 1)

    # ── Mood determination ──────────────────────────────────────────────────
    old_mood = getattr(agent, "system_mood", "neutral") or "neutral"

    if loss_streak >= 3 or win_rate < 25:
        mood = "danger"
    elif loss_streak >= 2 or win_rate < 40:
        mood = "cautious"
    elif win_rate > 55 and pf > 1.2:
        mood = "confident"
    else:
        mood = "neutral"

    notes = (
        f"win_rate={win_rate:.1f}% pf={pf:.2f} "
        f"loss_streak={loss_streak} avg_pnl={avg_pnl:+.4f}"
    )

    # ── Param change impact check ───────────────────────────────────────────
    verdict = "hold"
    param_history = list(getattr(agent, "param_history", None) or [])

    if param_history and loss_streak >= 3:
        last_change = param_history[-1]
        change_trade_count = int(last_change.get("trade_count", 0))
        current_trade_count = int(agent.total_trades or 0)
        trades_since_change = current_trade_count - change_trade_count

        if trades_since_change >= 5:
            old_params = last_change.get("params", {})
            if old_params:
                # The change didn't help — revert and note it
                verdict = "revert"
                agent.strategy_params = {**(agent.strategy_params or {}), **old_params}
                agent.param_history = param_history[:-1]
                notes = (
                    f"Reverted params after {trades_since_change} trades "
                    f"(loss_streak={loss_streak}). {notes}"
                )
                logger.info(
                    "strategy_guardian: agent {} REVERTED params "
                    "(loss_streak={} trades_since_change={})",
                    agent.id, loss_streak, trades_since_change,
                )

    # ── Update agent ────────────────────────────────────────────────────────
    agent.system_mood = mood
    agent.guardian_verdict = verdict
    agent.guardian_notes = notes
    agent.last_guardian_review_at = datetime.now(timezone.utc)
    await agent.save()

    # ── Notifications on mood change ────────────────────────────────────────
    if mood != old_mood:
        if mood == "danger":
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} — DANGER MODE",
                message=(
                    f"⛔ {loss_streak} consecutive losses. Win rate: {win_rate:.1f}%. "
                    "AI entering high-caution mode — only the clearest setups approved."
                ),
                data={"agent_id": str(agent.id), "trigger": "mood_danger"},
            )
        elif mood == "cautious":
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} — Cautious Mode",
                message=(
                    f"⚠️ Win rate dropped to {win_rate:.1f}%. "
                    "AI raising the bar for trade approvals."
                ),
                data={"agent_id": str(agent.id), "trigger": "mood_cautious"},
            )
        elif mood == "confident" and old_mood in ("cautious", "danger"):
            await NotificationService.create(
                user_id=agent.user_id,
                type="agent_status",
                title=f"{agent.name} — Back in Form",
                message=(
                    f"✅ Win rate {win_rate:.1f}%, PF {pf:.2f}. "
                    "Performance restored — AI returning to standard operation."
                ),
                data={"agent_id": str(agent.id), "trigger": "mood_confident"},
            )

    logger.info(
        "strategy_guardian: agent {} mood={} (was={}) verdict={} "
        "win_rate={:.1f}% loss_streak={} pf={:.2f}",
        agent.id, mood, old_mood, verdict, win_rate, loss_streak, pf,
    )


def snapshot_params(agent: Agent) -> None:
    """Snapshot current strategy_params before applying any change.

    Call this BEFORE modifying agent.strategy_params so the guardian can revert
    if the change turns out to hurt performance. Keeps at most 5 snapshots.
    """
    history = list(getattr(agent, "param_history", None) or [])
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "params": dict(agent.strategy_params or {}),
        "trade_count": int(agent.total_trades or 0),
    }
    history.append(snapshot)
    agent.param_history = history[-5:]
