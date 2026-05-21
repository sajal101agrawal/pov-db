from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.pool import get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.services.cache import CacheService
from app.services.factory import build_bhavcopy_source
from app.services.live import fetch_and_store_live_quotes, fetch_and_store_live_snapshots
from app.sources.rates import IndiaRiskFreeRateClient


router = APIRouter()


async def repository() -> MarketRepository:
    return MarketRepository(await get_pool())


async def cache(settings: Settings = Depends(get_settings)) -> CacheService:
    return CacheService(Redis.from_url(settings.redis_url))


@router.get("/health")
async def health(repo: MarketRepository = Depends(repository)) -> dict:
    latest = await repo.latest_trade_date()
    return {"ok": True, "latest_trade_date": latest}


@router.get("/symbols")
async def symbols(repo: MarketRepository = Depends(repository)) -> list[str]:
    return await repo.active_symbols()


@router.get("/symbol/{symbol}")
async def symbol_dashboard(
    symbol: str,
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    cached = await cache_service.get_dashboard(symbol)
    if cached is None:
        cached = await repo.dashboard_row(symbol)
        if cached is None:
            raise HTTPException(status_code=404, detail="symbol not found")
        await cache_service.set_dashboard(symbol, cached)
    live = await cache_service.get_live(symbol)
    return {**cached, **live}


@router.get("/symbol/{symbol}/history")
async def symbol_history(
    symbol: str,
    days: int = Query(default=365, ge=1, le=2000),
    repo: MarketRepository = Depends(repository),
) -> list[dict]:
    return await repo.history(symbol, days)


@router.get("/symbol/{symbol}/pnl")
async def symbol_pnl(
    symbol: str,
    days: int = Query(default=365, ge=1, le=2000),
    repo: MarketRepository = Depends(repository),
) -> list[dict]:
    return await repo.straddle_history(symbol, days)


@router.get("/live/{symbol}")
async def live(symbol: str, cache_service: CacheService = Depends(cache)) -> dict:
    payload = await cache_service.get_live(symbol)
    if not payload:
        raise HTTPException(status_code=404, detail="live data not available")
    return payload


@router.get("/live")
async def live_symbols(cache_service: CacheService = Depends(cache)) -> list[dict]:
    return await cache_service.get_live_symbols()


@router.get("/live/{symbol}/option-chain")
async def live_option_chain(
    symbol: str,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    cached = await cache_service.get_live(f"chain:{symbol}")
    if cached:
        return cached
    redis = Redis.from_url(settings.redis_url)
    try:
        result = await fetch_and_store_live_snapshots(settings, repo, redis, [symbol.upper()])
        payload = await CacheService(redis).get_live(f"chain:{symbol}")
        if payload:
            return payload
        raise HTTPException(status_code=404, detail=result)
    finally:
        await redis.aclose()


@router.post("/admin/live-quotes")
async def trigger_live_quotes(
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    redis = Redis.from_url(settings.redis_url)
    try:
        symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
        return await fetch_and_store_live_quotes(settings, repo, redis, symbol_list)
    except Exception as exc:
        await repo.log_error(
            "api_live_quotes",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            source="dhan",
        )
        raise
    finally:
        await redis.aclose()


@router.post("/admin/live-snapshot")
async def trigger_live_snapshot(
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    redis = Redis.from_url(settings.redis_url)
    try:
        symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
        return await fetch_and_store_live_snapshots(settings, repo, redis, symbol_list)
    except Exception as exc:
        await repo.log_error(
            "api_live_snapshot",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            source="dhan",
        )
        raise
    finally:
        await redis.aclose()


@router.post("/admin/trigger-pipeline")
async def trigger_pipeline(
    trade_date: date,
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(default_rate=settings.default_risk_free_rate),
    )
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    try:
        return await pipeline.run_for_date(trade_date, symbol_list, finalize=True)
    except Exception as exc:
        await repo.log_error(
            "api_trigger_pipeline",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbol_list},
            trade_date=trade_date,
            source="admin_api",
        )
        raise
