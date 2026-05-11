from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.portfolio import CandlePoint, MarketTicker
from app.services.market_data import get_public_candles, get_public_ticker

router = APIRouter()

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


@router.get("/tickers", response_model=list[MarketTicker])
async def tickers(
    symbols: list[str] = Query(default=None),
    user: User = Depends(get_current_user),
):
    out: list[MarketTicker] = []
    for s in symbols or DEFAULT_SYMBOLS:
        try:
            t = await get_public_ticker(s)
        except Exception:  # noqa: BLE001
            continue
        out.append(MarketTicker(**t))
    return out


@router.get("/candles", response_model=list[CandlePoint])
async def candles(
    symbol: str = Query(..., min_length=3),
    interval: str = Query(default="15m"),
    limit: int = Query(default=200, ge=10, le=500),
    user: User = Depends(get_current_user),
):
    try:
        rows = await get_public_candles(symbol, interval, limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"market data error: {exc}")
    return [CandlePoint(**r) for r in rows]
