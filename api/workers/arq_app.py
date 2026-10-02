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
from api.alerts.send import send_alerts
from api.alerts.watchdog import note_llm, watchdog
from api.billing.jobs import pull_meta_statements
from api.config import get_settings
from api.core.logging import configure_logging, get_logger
from api.db.models import TenantChannel
from api.db.session import Database
from api.llm.embeddings import FastEmbedder
from api.llm.router import LLMRouter
from api.llm.transcribe import GeminiTranscriber
from api.meta.client import MetaClient
from api.meta.outbound import client_for_channel
from api.modules.campaigns.jobs import campaign_housekeeping, poll_templates, refresh_quality
from api.modules.campaigns.sender import run_campaign
from api.ops.jobs import nightly_backup
from api.workers.jobs.escalation import notify_escalation
from api.workers.jobs.noop import noop
from api.workers.jobs.recovery import drain_webhook_buffer, requeue_stranded
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
    redis = ctx["redis"]

    async def llm_event(name: str) -> None:
        await note_llm(redis, name)

    llm = LLMRouter(http, settings, on_event=llm_event)

    def client_factory(channel: TenantChannel) -> MetaClient:
        return client_for_channel(channel, http, settings)

    ctx.update(
        settings=settings,
        db=db,
        http=http,
        llm=llm,
        client_factory=client_factory,
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
        run_campaign,
        refresh_quality,
        poll_templates,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = get_settings().arq_queue_name
    max_jobs = get_settings().worker_max_jobs
    # how often an idle worker looks for new jobs. arq's default (0.5 s) added ~0.25 s to every
    # reply on average; 0.1 s is 10 cheap Redis calls a second per worker (Phase 6 load test).
    poll_delay = 0.1
    job_timeout = 180  # transcription + up to 5 tool rounds + regeneration
    max_tries = 5
    keep_result = 3600
    health_check_interval = 30


class SchedulerSettings:
    functions: ClassVar[list[WorkerCoroutine]] = []
    cron_jobs: ClassVar[list[Any]] = [
        cron(scheduler_heartbeat, second=0, run_at_startup=False),
        # quality rating + pending template statuses, every 15 minutes (02 §4.4)
        cron(campaign_housekeeping, minute={0, 15, 30, 45}, second=30, run_at_startup=False),
        # Meta's own per-day charges, for reconciliation and the reimbursement ledger (Phase 5)
        cron(pull_meta_statements, hour=4, minute=30, second=0, run_at_startup=False),
        # Phase 6: recovery, alerting, backups
        cron(drain_webhook_buffer, second={5, 35}, run_at_startup=True),
        cron(requeue_stranded, minute=set(range(2, 60, 5)), second=10, run_at_startup=False),
        cron(watchdog, second=20, run_at_startup=False),
        cron(send_alerts, second=40, run_at_startup=False),
        cron(nightly_backup, hour=1, minute=30, second=0, run_at_startup=False, timeout=3600),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = get_settings().arq_scheduler_queue_name
    max_jobs = 5
    health_check_interval = 30
