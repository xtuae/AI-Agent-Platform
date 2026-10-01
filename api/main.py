"""FastAPI application: lifespan-managed DB engine and Redis pool, JSON logging, correlation ids."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from arq import create_pool
from fastapi import FastAPI
from redis.asyncio import Redis

from api import health
from api.api import v1
from api.auth import routes as auth_routes
from api.config import get_settings
from api.core.logging import configure_logging, get_logger
from api.core.middleware import CorrelationIdMiddleware
from api.db.session import Database
from api.events import EventBroker
from api.llm.router import LLMRouter
from api.optin import public as optin_public
from api.platform import auth as platform_auth
from api.platform import console as platform_console
from api.webhooks import meta as meta_webhook
from api.webhooks.ingest import WebhookIngestor
from api.webhooks.router import TenantRouter
from api.workers.arq_app import redis_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.db = Database(settings)
    app.state.redis = Redis.from_url(
        str(settings.redis_url),
        socket_timeout=settings.redis_timeout_s,
        socket_connect_timeout=settings.redis_timeout_s,
        health_check_interval=30,
    )
    app.state.arq = await create_pool(redis_settings(), default_queue_name=settings.arq_queue_name)
    app.state.router = TenantRouter(
        app.state.db,
        app.state.redis,
        ttl_s=settings.webhook_route_cache_ttl_s,
        negative_ttl_s=settings.webhook_route_negative_ttl_s,
    )
    app.state.ingestor = WebhookIngestor(
        app.state.db,
        app.state.redis,
        app.state.router,
        app.state.arq,
        dedup_ttl_s=settings.webhook_dedup_ttl_s,
    )
    # Outbound calls from the API (dashboard replies). Every request has explicit timeouts via
    # MetaClient; the pool limit keeps a stuck Graph API from exhausting sockets.
    app.state.http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        timeout=httpx.Timeout(
            settings.meta_http_timeout_s, connect=settings.meta_connect_timeout_s
        ),
    )
    # dashboard-side LLM use only (the campaign copy drafter); the agent runs in workers
    app.state.llm = LLMRouter(app.state.http, settings)
    app.state.events = EventBroker(
        str(settings.database_url), connect_timeout_s=settings.db_connect_timeout_s
    )
    app.state.events.start()
    if settings.jwt_secret is None:
        log.error("jwt_secret_missing", effect="dashboard login will answer 503")
    if settings.meta_app_secret is None:
        log.error("meta_app_secret_missing", effect="every webhook POST will be rejected with 403")
    log.info(
        "startup", env=settings.app_env, version=settings.app_version, git_sha=settings.git_sha
    )
    try:
        yield
    finally:
        await app.state.events.stop()
        await app.state.http.aclose()
        await app.state.arq.aclose()
        await app.state.redis.aclose()
        await app.state.db.dispose()
        log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="HMH Labz Agent Platform",
        version=settings.app_version,
        lifespan=lifespan,
        # No public schema in production; the dashboard is the only API client.
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(meta_webhook.router)
    app.include_router(auth_routes.router)
    app.include_router(v1.build_router())
    app.include_router(optin_public.router)
    app.include_router(platform_auth.router)
    app.include_router(platform_console.router)
    return app


app = create_app()
