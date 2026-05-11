from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.strategy import Strategy
from app.models.user import User
from app.schemas.agent import (
    AgentControlAction,
    AgentCreate,
    AgentPublic,
    AgentUpdate,
    StrategyPublic,
)

router = APIRouter()


@router.get("/strategies", response_model=list[StrategyPublic])
async def list_strategies(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Strategy).order_by(Strategy.name))
    return result.scalars().all()


@router.get("", response_model=list[AgentPublic])
async def list_agents(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Agent).where(Agent.user_id == user.id).order_by(Agent.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=AgentPublic, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Validate ownership of the api_key + strategy.
    api_key = await db.get(ApiKey, body.api_key_id)
    if not api_key or api_key.user_id != user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid api_key_id")
    strategy = await db.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid strategy_id")

    # Defaults from strategy if user didn't override.
    params = {**(strategy.default_params or {}), **(body.strategy_params or {})}
    agent = Agent(
        user_id=user.id,
        api_key_id=body.api_key_id,
        strategy_id=body.strategy_id,
        name=body.name,
        description=body.description,
        assigned_capital=body.assigned_capital,
        currency=body.currency,
        trading_pairs=list(body.trading_pairs),
        timeframe=body.timeframe,
        max_risk_per_trade=body.max_risk_per_trade,
        daily_profit_target=body.daily_profit_target,
        weekly_profit_target=body.weekly_profit_target,
        max_daily_loss=body.max_daily_loss,
        max_concurrent_trades=body.max_concurrent_trades,
        max_consecutive_losses=body.max_consecutive_losses,
        strategy_params=params,
        ai_optimization_enabled=body.ai_optimization_enabled,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


async def _load_agent(db: AsyncSession, agent_id: uuid.UUID, user: User) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent or agent.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@router.get("/{agent_id}", response_model=AgentPublic)
async def get_agent(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _load_agent(db, agent_id, user)


@router.patch("/{agent_id}", response_model=AgentPublic)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _load_agent(db, agent_id, user)
    if agent.status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "pause the agent before editing its parameters"
        )
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(agent, field, value)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _load_agent(db, agent_id, user)
    await db.delete(agent)
    await db.commit()


@router.post("/{agent_id}/control", response_model=AgentPublic)
async def control_agent(
    agent_id: uuid.UUID,
    body: AgentControlAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _load_agent(db, agent_id, user)
    if body.action == "start":
        if not agent.api_key_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent has no api key")
        if (agent.assigned_capital or 0) <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "assign capital before starting")
        agent.status = "active"
        agent.started_at = datetime.now(timezone.utc)
    elif body.action == "pause":
        agent.status = "paused"
    else:  # stop
        agent.status = "stopped"
    await db.commit()
    await db.refresh(agent)
    return agent
