from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai,
    api_keys,
    auth,
    market,
    notifications,
    portfolio,
    trades,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(api_keys.router, prefix="/api-keys", tags=["api-keys"])
api_router.include_router(agents.router, prefix="/agents", tags=["agents"])
api_router.include_router(trades.router, prefix="/trades", tags=["trades"])
api_router.include_router(portfolio.router, prefix="/portfolio", tags=["portfolio"])
api_router.include_router(market.router, prefix="/market", tags=["market"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
