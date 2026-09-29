"""/api/v1 — the dashboard API. Every route here takes its tenant from the access token."""

from fastapi import APIRouter

from api.api.v1 import conversations, customers, orders, settings, stream, today

router = APIRouter(prefix="/api/v1")
for module in (today, orders, conversations, customers, settings, stream):
    router.include_router(module.router)

__all__ = ["router"]
