from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.models.agent import Agent, StrategyEmbed
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
async def list_strategies(user: User = Depends(get_current_user)):
    return await Strategy.find_all().sort(+Strategy.name).to_list()


@router.get("", response_model=list[AgentPublic])
async def list_agents(user: User = Depends(get_current_user)):
    return await Agent.find(Agent.user_id == user.id).sort(-Agent.created_at).to_list()


@router.post("", response_model=AgentPublic, status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentCreate, user: User = Depends(get_current_user)):
    api_key = await ApiKey.find_one(ApiKey.id == body.api_key_id, ApiKey.user_id == user.id)
    if not api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid api_key_id")
    strategy = await Strategy.get(body.strategy_id)
    if not strategy:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid strategy_id")

    params = {**(strategy.default_params or {}), **(body.strategy_params or {})}
    agent = Agent(
        user_id=user.id,
        api_key_id=body.api_key_id,
        strategy_id=body.strategy_id,
        strategy=StrategyEmbed(
            id=strategy.id,
            name=strategy.name,
            type=strategy.type,
            description=strategy.description or "",
            default_params=strategy.default_params or {},
        ),
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
    await agent.insert()
    return agent


async def _load_agent(agent_id: uuid.UUID, user: User) -> Agent:
    agent = await Agent.find_one(Agent.id == agent_id, Agent.user_id == user.id)
    if not agent:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@router.get("/{agent_id}", response_model=AgentPublic)
async def get_agent(agent_id: uuid.UUID, user: User = Depends(get_current_user)):
    return await _load_agent(agent_id, user)


@router.patch("/{agent_id}", response_model=AgentPublic)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    user: User = Depends(get_current_user),
):
    agent = await _load_agent(agent_id, user)
    if agent.status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "pause the agent before editing its parameters"
        )
    if body.api_key_id is not None:
        key = await ApiKey.find_one(ApiKey.id == body.api_key_id, ApiKey.user_id == user.id)
        if not key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid api_key_id")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(agent, field, value)
    await agent.save()
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, user: User = Depends(get_current_user)):
    agent = await _load_agent(agent_id, user)
    await agent.delete()


@router.post("/{agent_id}/control", response_model=AgentPublic)
async def control_agent(
    agent_id: uuid.UUID,
    body: AgentControlAction,
    user: User = Depends(get_current_user),
):
    agent = await _load_agent(agent_id, user)
    if body.action == "start":
        if not agent.api_key_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "agent has no api key")
        if (agent.assigned_capital or 0) <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "assign capital before starting")
        agent.status = "active"
        agent.started_at = datetime.now(timezone.utc)
    elif body.action == "pause":
        agent.status = "paused"
    else:
        agent.status = "stopped"
    await agent.save()
    return agent
