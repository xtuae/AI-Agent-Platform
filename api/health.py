"""GET /health — {status, db, redis, version, git_sha}. 200 when healthy, 503 when degraded.

HEAD /health gives the same status code without the body: uptime monitors such as
UptimeRobot's free plan can only send HEAD, and a GET-only route answers them 405.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from api.config import get_settings
from api.core.logging import get_logger
from api.deps import DatabaseDep, RedisDep

router = APIRouter()
log = get_logger(__name__)

CHECK_TIMEOUT_S = 2.0

Check = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Check
    redis: Check
    version: str
    git_sha: str


async def _check_db(db: DatabaseDep) -> Check:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            return "ok" if await db.ping() else "error"
    except (TimeoutError, SQLAlchemyError, OSError) as exc:
        log.warning("health_db_failed", error=type(exc).__name__)
        return "error"


async def _check_redis(redis: RedisDep) -> Check:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            return "ok" if await redis.ping() else "error"
    except (TimeoutError, RedisError, OSError) as exc:
        log.warning("health_redis_failed", error=type(exc).__name__)
        return "error"


@router.get("/health", response_model=HealthResponse)
@router.head("/health", include_in_schema=False)
async def health(response: Response, db: DatabaseDep, redis: RedisDep) -> HealthResponse:
    db_status, redis_status = await asyncio.gather(_check_db(db), _check_redis(redis))
    healthy = db_status == "ok" and redis_status == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    settings = get_settings()
    return HealthResponse(
        status="ok" if healthy else "degraded",
        db=db_status,
        redis=redis_status,
        version=settings.app_version,
        git_sha=settings.git_sha,
    )
