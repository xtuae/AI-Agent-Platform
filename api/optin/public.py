"""Public consent pages: GET /q/<code> shows the wording, POST /q/<code> records the visit and
redirects to WhatsApp. No login, no cookies, no script; nothing about the visitor is stored.

A link preview (WhatsApp, iMessage) or crawler only ever GETs, so it never mints a ref. POSTs are
rate-limited per client address to keep the visits table from being flooded.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, select_autoescape
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from api.config import get_settings
from api.core.logging import get_logger
from api.db.models import OptinLink, Tenant
from api.db.session import Database
from api.deps import get_database, get_redis
from api.optin.service import prefilled, record_visit, wa_link, whatsapp_number

router = APIRouter(tags=["optin-public"], include_in_schema=False)
log = get_logger(__name__)

LIVE_TENANT_STATUSES: Final = ("trial", "active")
HEADERS: Final = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://wa.me "
        "https://api.whatsapp.com; frame-ancestors 'none'; base-uri 'none'"
    ),
}
COPY: Final = {
    "en": {
        "button": "Continue on WhatsApp",
        "note": "WhatsApp will open with a message ready for you to send. Nothing is signed up "
        "until you send it. Reply STOP at any time to stop receiving offers.",
        "gone": "This link is no longer active.",
    },
    "ar": {
        "button": "المتابعة على واتساب",
        "note": "سيفتح واتساب ومعه رسالة جاهزة للإرسال. لن يتم تسجيلك إلا بعد إرسالها. "
        "أرسل STOP في أي وقت لإيقاف العروض.",
        "gone": "هذا الرابط لم يعد فعالاً.",
    },
}

_env = Environment(autoescape=select_autoescape(default=True), trim_blocks=True)
PAGE = _env.from_string(
    """<!doctype html>
<html lang="{{ lang }}" dir="{{ 'rtl' if lang == 'ar' else 'ltr' }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{{ business }}</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#5b6370;
          --brand:#128c4a; --on-brand:#fff; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f1115; --card:#181b21; --ink:#eef0f3;
          --muted:#a3abb8; --brand:#25b163; --on-brand:#06240f; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:var(--bg); color:var(--ink); padding:16px;
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", Tahoma, sans-serif; }
  main { width:100%; max-width:420px; background:var(--card); border-radius:16px; padding:24px;
         box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .biz { font-size:14px; color:var(--muted); margin:0 0 4px; }
  h1 { font-size:22px; line-height:1.3; margin:0 0 16px; }
  .wording { margin:0 0 24px; white-space:pre-line; }
  button { width:100%; border:0; border-radius:12px; padding:14px 16px; font:inherit;
           font-weight:600; background:var(--brand); color:var(--on-brand); cursor:pointer; }
  .note { font-size:13px; color:var(--muted); margin:16px 0 0; }
</style>
</head>
<body>
<main>
{% if gone %}
  <p class="biz">{{ business }}</p>
  <h1>{{ copy.gone }}</h1>
{% else %}
  <p class="biz">{{ business }}</p>
  <h1>{{ heading }}</h1>
  <p class="wording">{{ wording }}</p>
  <form method="post" action="/q/{{ code }}">
    <button type="submit">{{ copy.button }}</button>
  </form>
  <p class="note">{{ copy.note }}</p>
{% endif %}
</main>
</body>
</html>
"""
)


async def _live_link(db: Database, code: str) -> tuple[OptinLink, Tenant, str] | None:
    if not code.isalnum() or not 6 <= len(code) <= 16:
        return None
    async with db.platform_session() as s:
        row = (
            await s.execute(
                select(OptinLink, Tenant)
                .join(Tenant, Tenant.id == OptinLink.tenant_id)
                .where(
                    OptinLink.code == code,
                    OptinLink.is_active.is_(True),
                    Tenant.status.in_(LIVE_TENANT_STATUSES),
                )
            )
        ).first()
        if row is None:
            return None
        phone = await whatsapp_number(s, row[0].tenant_id)
    if phone is None:
        return None
    return row[0], row[1], phone


def _gone() -> HTMLResponse:
    html = PAGE.render(lang="en", business="", gone=True, copy=COPY["en"])
    return HTMLResponse(html, status_code=404, headers=HEADERS)


@router.get("/q/{code}", response_class=HTMLResponse)
async def page(code: str, db: Annotated[Database, Depends(get_database)]) -> Response:
    found = await _live_link(db, code)
    if found is None:
        return _gone()
    link, tenant, _ = found
    html = PAGE.render(
        lang=link.language,
        business=tenant.name,
        heading=link.heading,
        wording=link.wording,
        code=link.code,
        copy=COPY[link.language],
        gone=False,
    )
    return HTMLResponse(html, headers=HEADERS)


async def _allowed(redis: Redis, request: Request) -> bool:
    settings = get_settings()
    host = request.client.host if request.client else "unknown"
    key = "optin:rl:" + hashlib.sha256(host.encode()).hexdigest()[:24]
    try:
        async with redis.pipeline(transaction=True) as p:
            p.incr(key)
            p.expire(key, settings.optin_rate_window_s, nx=True)
            count, _ = await p.execute()
    except (RedisError, OSError) as exc:
        log.warning("optin_rate_limit_unavailable", error=type(exc).__name__)
        return True
    return int(count) <= settings.optin_rate_limit


@router.post("/q/{code}")
async def go(
    code: str,
    request: Request,
    db: Annotated[Database, Depends(get_database)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    found = await _live_link(db, code)
    if found is None:
        return _gone()
    link, _, phone = found
    if not await _allowed(redis, request):
        return Response("Too many requests — try again in a few minutes.", status_code=429,
                        headers=HEADERS)  # fmt: skip
    async with db.tenant_session(link.tenant_id) as s:
        visit = await record_visit(s, link)
        ref = visit.token
    log.info("optin_visit", tenant_id=str(link.tenant_id), link_id=str(link.id))
    return RedirectResponse(wa_link(phone, prefilled(link, ref)), status_code=303, headers=HEADERS)
