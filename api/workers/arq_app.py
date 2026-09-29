"""ARQ entrypoints.

    worker:     arq api.workers.arq_app.WorkerSettings
    scheduler:  arq api.workers.arq_app.SchedulerSettings

The scheduler listens on its own queue so it never picks up (and fails) jobs meant for the worker;
its cron jobs enqueue work onto the main queue rather than doing heavy work themselves.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from arq import cron
from arq.connections import RedisSettings
from arq.typing import WorkerCoroutine

from api.agents.turn import TurnRunner
from api.config import get_settings
from api.core.logging import configure_logging, get_logger
from api.db.models import TenantChannel
from api.db.session import Database
from api.llm.embeddings import FastEmbedder
from api.llm.router import LLMRouter
from api.llm.transcribe import GeminiTranscriber
from api.meta.client import MetaClient
from api.meta.outbound import client_for_channel
from api.workers.jobs.escalation import notify_escalation
from api.workers.jobs.noop import noop
from api.workers.jobs.summarise import summarise_conversation
from api.workers.jobs.turn import handle_inbound_message
from api.workers.jobs.webhook_events import apply_status_event, apply_template_status_event

log = get_logger(__name__)


def redis_settings() -> RedisSettings:
    s = get_settings()
    return RedisSettings.from_dsn(str(s.redis_url))


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    db = Database(settings)
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    llm = LLMRouter(http, settings)

    def client_factory(channel: TenantChannel) -> MetaClient:
        return client_for_channel(channel, http, settings)

    ctx.update(
        settings=settings,
        db=db,
        http=http,
        llm=llm,
        turn_runner=TurnRunner(
            db=db,
            redis=ctx["redis"],
            llm=llm,
            settings=settings,
            client_factory=client_factory,
            embedder=FastEmbedder(settings.embedding_model, settings.embedding_cache_dir),
            transcriber=GeminiTranscriber(http, settings),
            enqueuer=ctx["redis"],
        ),
    )
    log.info("worker_startup", version=settings.app_version)


async def shutdown(ctx: dict[str, Any]) -> None:
    http: httpx.AsyncClient | None = ctx.get("http")
    if http is not None:
        await http.aclose()
    db: Database | None = ctx.get("db")
    if db is not None:
        await db.dispose()
    log.info("worker_shutdown")


async def scheduler_heartbeat(ctx: dict[str, Any]) -> None:
    """Every minute: prove the scheduler is alive by enqueueing a no-op onto the main queue."""
    await ctx["redis"].enqueue_job(
        "noop", "scheduler-heartbeat", _queue_name=get_settings().arq_queue_name
    )


class WorkerSettings:
    functions: ClassVar[list[WorkerCoroutine]] = [
        noop,
        handle_inbound_message,
        apply_status_event,
        apply_template_status_event,
        summarise_conversation,
        notify_escalation,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = get_settings().arq_queue_name
    max_jobs = 20
    job_timeout = 180  # transcription + up to 5 tool rounds + regeneration
    max_tries = 5
    keep_result = 3600
    health_check_interval = 30


class SchedulerSettings:
    functions: ClassVar[list[WorkerCoroutine]] = []
    cron_jobs: ClassVar[list[Any]] = [cron(scheduler_heartbeat, second=0, run_at_startup=False)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = get_settings().arq_scheduler_queue_name
    max_jobs = 5
    health_check_interval = 30
