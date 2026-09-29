"""/api/v1 — the dashboard API. Every route here takes its tenant from the access token.

Core routes live in this package. Each enabled module's router is mounted at /api/v1/m/<key>/
behind `module_enabled(key)`, which answers 404 for a tenant without that module.
"""

from fastapi import APIRouter, Depends

from api.api.v1 import contacts, conversations, settings, stream, today
from api.auth.deps import module_enabled


def build_router() -> APIRouter:
    from api.modules import registry

    router = APIRouter(prefix="/api/v1")
    for module in (today, conversations, contacts, settings, stream):
        router.include_router(module.router)
    for key, mod in registry.all_modules().items():
        if mod.router is not None:
            router.include_router(
                mod.router,
                prefix=f"/m/{key}",
                tags=[f"module:{key}"],
                dependencies=[Depends(module_enabled(key))],
            )
    return router


__all__ = ["build_router"]
