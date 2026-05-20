from __future__ import annotations

import traceback
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


settings = get_settings()
app = FastAPI(title="Power of Volatility DB API", version="0.1.0")
app.include_router(router, prefix=settings.api_prefix)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = str(uuid4())
    try:
        repo = MarketRepository(await get_pool())
        await repo.log_error(
            task_name="api_request",
            source=str(request.url.path),
            error_type=type(exc).__name__,
            error_details={
                "error_id": error_id,
                "method": request.method,
                "path": str(request.url.path),
                "query": str(request.url.query),
                "message": str(exc),
                "traceback": traceback.format_exc(limit=20),
            },
        )
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": "internal server error", "error_id": error_id})


@app.on_event("shutdown")
async def shutdown() -> None:
    await close_pool()
