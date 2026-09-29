"""The ARQ queue works end to end: enqueue → worker picks up → result stored."""

from __future__ import annotations

import uuid

from arq import create_pool
from arq.worker import Worker

from api.workers.arq_app import redis_settings, shutdown, startup
from api.workers.jobs.noop import noop


async def test_noop_job_round_trip(migrated: None) -> None:
    queue = f"arq:test:{uuid.uuid4().hex}"  # isolated queue; never collides with a live worker
    pool = await create_pool(redis_settings())
    try:
        job = await pool.enqueue_job("noop", "phase-0", _queue_name=queue)
        assert job is not None

        worker = Worker(
            functions=[noop],
            queue_name=queue,
            redis_settings=redis_settings(),
            on_startup=startup,
            on_shutdown=shutdown,
            burst=True,
            poll_delay=0.05,
            handle_signals=False,
        )
        try:
            await worker.main()
        finally:
            await worker.close()

        assert await job.result(timeout=5) == {"marker": "phase-0", "db_ok": True}
    finally:
        await pool.aclose()
