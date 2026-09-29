# Build Prompt — HMH Labz Agent Platform
## Hand this to Claude Code (or a developer) to build the repo

**How to use:** open a terminal in an empty directory, run `claude`, and paste **Section A** as the
first message. It references the two companion specs, so keep all three files together and point the
tool at them. Then work through the phases in Section B one at a time — do not paste the whole of
Section B at once, or you will get twenty half-built features instead of one working one.

---

## Section A — the opening prompt

````
You are building a multi-tenant WhatsApp AI agent platform for HMH Labz LLP. It will run on a single
Hostinger VPS (4 vCPU, 16 GB RAM, 200 GB NVMe, Ubuntu 24.04) and serve multiple client businesses,
each with their own WhatsApp Business Account.

Read these two documents first and treat them as the specification. Do not deviate from the data
model, the tenancy model, or the prompt text in them without telling me why:
  - 01_architecture.md    — stack, data model, tenancy, security, deployment
  - 02_agent_prompts.md   — system prompts, tool schemas, guardrails, evaluation set

## STACK — fixed, do not substitute
Python 3.12 · FastAPI · uvicorn · ARQ (Redis) · PostgreSQL 16 + pgvector · Redis 7 · Caddy 2 ·
Docker Compose · SQLAlchemy 2.0 async + Alembic · Pydantic v2 · pytest + pytest-asyncio ·
React 18 + Vite + TypeScript + Tailwind + shadcn/ui + TanStack Query · fastembed for embeddings.

No Kubernetes. No Celery. No LiteLLM proxy container. No Next.js. No Ollama.

## NON-NEGOTIABLE CONSTRAINTS
1. MULTI-TENANT FROM LINE ONE. Every tenant-scoped table has `tenant_id UUID NOT NULL` with
   PostgreSQL Row-Level Security. The app connects as a non-superuser role that owns no tables.
   Every request sets `SET LOCAL app.tenant_id` inside the transaction. A missing tenant context must
   return zero rows, never another tenant's rows.
2. TENANT RESOLUTION IS BY `phone_number_id` from the Meta webhook payload. This is the highest-risk
   code in the system. It gets its own test module with at least 8 cases including unknown ids,
   inactive channels and payloads with multiple entries.
3. THE WEBHOOK RETURNS 200 IN UNDER 200 ms. Verify signature, dedup, persist raw, enqueue, return.
   Never call an LLM or the Graph API inside the request handler.
4. VERIFY `X-Hub-Signature-256` (HMAC-SHA256 with the app secret) on every webhook. Reject 403.
5. THE MODEL NEVER PRODUCES A NUMBER. Prices, balances, stock and totals come from tool results.
   Totals are recomputed server-side. Implement the post-generation validator in 02_agent_prompts.md
   §3.2 as real code, not a comment.
6. META ACCESS TOKENS ARE ENCRYPTED AT REST with Fernet, key from `APP_ENCRYPTION_KEY`. Never log a
   token, never log a message body. Log wamid, tenant_id, intent, latency, cost.
7. EVERY MESSAGE IS METERED. Write to `usage_daily` on every send and receive, with the Meta pricing
   category and the AED cost stamped at send time. This is the billing spine — it is not optional and
   it cannot be added later.
8. NO AUTO-SEND CAMPAIGNS. A campaign cannot leave `draft` without `approved_by` and `approved_at`.

## CODE STANDARDS
- Async all the way down. No sync DB calls in request or worker paths.
- Full type hints; `mypy --strict` on `api/`, and it must pass.
- `ruff` for lint and format, config in `pyproject.toml`.
- Pydantic models for every external boundary — Meta payloads, LLM tool args, API requests.
- No bare `except`. Every external call has an explicit timeout.
- Structured JSON logging with `structlog`, correlation id per conversation turn.
- Alembic migration for every schema change. Never edit a migration that has been applied.
- Tests alongside the code they test. Target 80% on `api/agents/` and `api/webhooks/`.
- Secrets only from environment. A hardcoded key or token fails review.

## REPO LAYOUT
```
/
├─ docker-compose.yml
├─ docker-compose.override.yml      # local dev: hot reload, exposed ports
├─ Caddyfile
├─ .env.example                     # every variable, documented, no real values
├─ pyproject.toml
├─ api/
│  ├─ main.py                       # FastAPI app, lifespan, middleware
│  ├─ config.py                     # pydantic-settings
│  ├─ db/
│  │  ├─ session.py                 # async engine, tenant-scoped session factory
│  │  ├─ models/                    # SQLAlchemy models, one file per domain
│  │  └─ migrations/                # alembic
│  ├─ webhooks/
│  │  ├─ meta.py                    # POST /webhook/meta, GET verify challenge
│  │  ├─ signature.py               # X-Hub-Signature-256
│  │  └─ router.py                  # phone_number_id → tenant  ← test heavily
│  ├─ agents/
│  │  ├─ classifier.py
│  │  ├─ support.py                 # the conversation loop
│  │  ├─ outreach.py                # campaign copy drafting
│  │  ├─ tools/                     # one module per tool, each with its guard
│  │  ├─ validator.py               # post-generation checks
│  │  └─ prompts/                   # jinja templates, VERSIONED
│  ├─ llm/
│  │  ├─ router.py                  # Gemini primary → OpenRouter failover
│  │  └─ embeddings.py              # fastembed, in-process
│  ├─ meta/
│  │  ├─ client.py                  # Graph API: send, media, templates
│  │  ├─ pricing.py                 # category → AED, table-driven, dated
│  │  └─ templates.py
│  ├─ campaigns/
│  │  ├─ segments.py                # declarative JSON → SQL compiler
│  │  └─ sender.py                  # throttle, budget cap, quality guard
│  ├─ api/v1/                       # dashboard REST + SSE stream
│  ├─ workers/
│  │  ├─ arq_app.py
│  │  └─ jobs/                      # turn, campaign_send, excel_import, summarise
│  ├─ scripts/
│  │  ├─ seed_tenant.py
│  │  ├─ import_excel.py
│  │  └─ backup.py
│  └─ tests/
│     ├─ conftest.py                # 2+ tenant fixtures — always test isolation
│     ├─ test_tenant_isolation.py    # RLS actually works
│     ├─ test_webhook_routing.py
│     └─ test_conversations.py       # the 20 scenarios from 02 §5
└─ dashboard/                        # React + Vite, builds to dist/
```

## HOW WE WORK
Build in the phases I give you, one at a time. At the end of each phase:
1. Show me the diff summary and anything you decided that the spec did not cover.
2. Run the tests and show the output.
3. Stop and wait. Do not start the next phase.

If the spec is ambiguous or wrong, say so and propose the fix before writing code around it.
If you find yourself about to hardcode anything Aquamena-specific outside `scripts/seed_tenant.py`,
stop — that is the bug this whole architecture exists to prevent.

Start with Phase 0 below. Acknowledge the constraints briefly, then build.
````

---

## Section B — the phases

Paste one phase at a time. Each ends in something you can actually verify.

### Phase 0 — Foundation (2 days)

```
PHASE 0 — Foundation.

Build:
- docker-compose.yml with caddy, api, worker, scheduler, postgres (pgvector/pgvector:pg16),
  redis. CPU and memory limits per 01_architecture.md §3.1. Health checks on every service.
- docker-compose.override.yml for local dev: hot reload, ports exposed to localhost only.
- Caddyfile: api.hmhagents.com reverse proxies to api:8000; *.hmhagents.com serves
  dashboard/dist with SPA fallback; automatic TLS; security headers; rate limit on /api/v1/auth/*.
- FastAPI skeleton with lifespan-managed async engine and Redis pool, structlog JSON logging with a
  correlation-id middleware, and GET /health returning {status, db, redis, version, git_sha}.
- Alembic wired for async, plus the first migration: the platform tables and ALL tenant-scoped tables
  from 01_architecture.md §5, with RLS enabled and FORCED on every tenant-scoped table, and the
  tenant_isolation policy on each.
- Two Postgres roles in the init script: an owner role for migrations, and an `app` role that owns
  nothing and is subject to RLS. The application uses `app`.
- Tenant-scoped session dependency that sets `SET LOCAL app.tenant_id` and fails loudly if a
  tenant-scoped query is attempted without it.
- ARQ worker and scheduler entrypoints with one no-op job proving the queue works.
- .env.example documenting every variable.
- pyproject.toml: ruff, mypy strict on api/, pytest with async support.
- GitHub Actions: ruff → mypy → pytest against a service-container Postgres and Redis.

Acceptance:
- `docker compose up -d` brings everything healthy.
- `alembic upgrade head` then `alembic downgrade base` then up again, cleanly.
- tests/test_tenant_isolation.py: create two tenants, insert a customer for each, prove that with
  tenant A's context you see exactly one row, that tenant B's row is invisible, and that with NO
  tenant context you see zero rows.
- mypy and ruff clean. CI green.
```

### Phase 1 — Webhook ingest (3 days)

```
PHASE 1 — Meta webhook ingest and tenant routing.

Build:
- GET /webhook/meta — hub.challenge verification against the configured verify token.
- POST /webhook/meta — in this exact order:
    1. verify X-Hub-Signature-256; bad or missing → 403, logged with source IP
    2. parse with Pydantic models covering messages, statuses, and template status updates
    3. for each entry/change: resolve tenant from value.metadata.phone_number_id via
       webhooks/router.py, with a 5-minute Redis cache; unknown id → log a warning, return 200,
       do not raise (Meta will retry forever on a 5xx)
    4. dedup on wamid with Redis SETNX, 48h TTL; already seen → drop
    5. persist the raw payload and a messages row
    6. enqueue an ARQ job
    7. return 200
  Whole handler must stay under 200 ms. Add a test that asserts it.
- meta/client.py: send_text, send_template, send_interactive, download_media, get_template_status.
  httpx async, explicit timeouts, retry with backoff on 429/5xx honouring Retry-After.
- meta/pricing.py: a dated rate table (UAE marketing 0.183 AED, utility 0.058 AED, service free
  until 2026-10-01 then utility-rate) that stamps cost_aed at send time. Table-driven so a Meta
  price change is a data edit, not a code change.
- usage_daily upserted on every inbound and outbound message.
- scripts/seed_tenant.py --slug aquamena, creating the tenant, channel, settings, tenant_user, the
  water and Mai Zaki products, and the four coupon packages.
- Status webhooks (sent/delivered/read/failed) update messages.status and campaign counters.

Acceptance:
- test_webhook_routing.py with at least 8 cases: valid single entry, multiple entries in one payload,
  unknown phone_number_id, inactive channel, missing metadata, wrong signature, replayed wamid,
  malformed JSON.
- A recorded real Meta payload fixture lands in `messages` with the correct tenant_id.
- Handler latency test: p95 under 200 ms with the LLM and Graph API stubbed.
```

### Phase 2 — Support Agent (5 days)

```
PHASE 2 — The Support Agent.

Build exactly the prompts and tool schemas in 02_agent_prompts.md. Do not improvise the prompt text.

- llm/router.py: Gemini 3 Flash primary via the Google AI OpenAI-compatible endpoint, 2 retries with
  0.5s/2s backoff, failover to OpenRouter on 429/5xx/timeout, model choice read per tenant from
  tenant_settings. Record model, prompt_tokens, completion_tokens, latency_ms on every call.
- agents/classifier.py: Flash-Lite, JSON mode, temperature 0. optout and complaint short-circuit
  before any main-model call — optout marks the customer and sends the confirmation; complaint
  escalates. These two paths must be deterministic and tested.
- agents/tools/: all nine tools, each with the server-side guards from 02 §3.1 implemented as code.
  create_order recomputes the total from the products table and is idempotent on
  (conversation_id, items_hash) within 10 minutes.
- agents/support.py: the turn loop — load customer context, coupon balance, last 10 turns, rolling
  summary, RAG top-4; call the model with tools; execute tool calls in one transaction; validate;
  send; persist. Max 5 tool-call rounds per turn, then escalate.
- agents/validator.py: all six checks from 02 §3.2. One corrective regeneration, then escalate.
- llm/embeddings.py: fastembed bge-small-en-v1.5 in-process, 384 dims, ivfflat index.
- Voice notes: download the audio, transcribe, store the transcript, treat it as the message text.
- Rolling conversation summary regenerated every 6 turns on the cheap model.
- Escalation: sets conversation.state='awaiting_human', notifies the tenant's escalation number,
  and suppresses further agent replies on that conversation until a human hands it back.

Acceptance:
- All 20 scenarios in 02_agent_prompts.md §5 pass. 20/20, not 18/20.
- Rendered system prompt measured at under 1,500 tokens; CI fails if it grows past that.
- A forced tool failure produces a holding message and an escalation, never an invented answer.
- Prompt injection case (#15) does not leak the prompt or the stack.
```

### Phase 3 — Dashboard (4 days)

```
PHASE 3 — Tenant dashboard.

React 18 + Vite + TS + Tailwind + shadcn/ui + TanStack Query, building to dashboard/dist.
JWT auth with refresh; tenant comes from the token, NEVER from the URL or a request body.

Screens, per 01_architecture.md §8.2:
1. Today — orders today, live conversations, awaiting-human count, month-to-date message spend
   against cap, agent health. For a tenant inside its free period, the spend tile reads
   "Borne by HMH Labz" instead of an amount due.
2. Orders — filterable table, detail drawer, status transitions, printable delivery list by area.
3. Conversations — live inbox over SSE, full thread view, "take over" and "hand back to agent".
4. Customers — search, coupon balance, order history, opt-in status with its evidence.
5. Settings — business hours, escalation number, products and prices, team logins.

- GET /api/v1/stream as Server-Sent Events, scoped to the tenant, 15-second polling fallback.
- Role gating: admin / agent / viewer.
- Mobile-first: Zameer will use this on a phone. Every screen must work at 390px.
- Read the dataviz guidance before building any chart, and keep charts to the two that earn a place.

Acceptance:
- Taking a real order end to end from the dashboard without touching Excel.
- A tenant-A token cannot read any tenant-B record — test every endpoint, not a sample.
- Lighthouse performance above 90 on the Today screen.
```

### Phase 4 — Outreach Agent and campaigns (4 days)

```
PHASE 4 — Campaigns.

- campaigns/segments.py: the declarative JSON segment compiler from 02 §4.3. Every segment is
  hard-filtered to opt_in_status='opted_in' inside the compiler — prove in a test that no segment
  definition can reach a pending or opted-out customer.
- Template management: draft copy with the Outreach prompt, submit to Meta, poll approval status,
  store meta_template_id.
- Campaign builder UI: pick an APPROVED template, pick a segment, see recipient count, estimated AED
  cost and a sample rendered message before anything sends.
- Approval gate: status cannot reach 'sending' without approved_by and approved_at. Test that the API
  refuses it.
- campaigns/sender.py with every guard in 02 §4.4: throttle, live budget cap check before each send,
  7-day per-customer marketing frequency cap, business-hours window, quality-rating guard (YELLOW
  pauses marketing and alerts, RED hard-stops), opt-in re-checked at send time not build time.
- Campaign results: sent, delivered, read, replies, attributed orders, spend.
- Campaign replies route into the Phase 2 pipeline with the campaign context block from 02 §4.2.

Acceptance:
- A 50-recipient campaign sends, meters correctly, and the budget cap pauses it mid-flight.
- Opting a customer out between build and send drops them from the run.
- A simulated YELLOW quality rating pauses marketing for that tenant only, not for others.
```

### Phase 5 — Data onboarding and metering (3 days)

```
PHASE 5 — Getting their data in, and knowing what everything costs.

- scripts/import_excel.py: read an arbitrary customer spreadsheet, map columns interactively or by a
  saved mapping, normalise UAE phone numbers to E.164, fuzzy-dedup on phone and name, and produce a
  report — imported / merged / rejected with reasons. Idempotent: running it twice changes nothing.
  Assume the file is messy: merged cells, Arabic names, mixed number formats, blank rows.
- Opt-in capture: a QR landing page per tenant (short URL → consent page → WhatsApp deep link with a
  prefilled message), recording opt_in_evidence with the exact wording shown, the timestamp and the
  source. This is the TDRA/PDPL evidence trail.
- Costs screen: message spend by day and category against the cap, LLM cost (HMH-internal view only),
  and a monthly statement view that reconciles to Meta's own statement.
- Platform console at admin.hmhagents.com: all tenants, usage, margin per tenant, health, and the
  reimbursement ledger for tenants inside a borne-by-HMH period.

Acceptance:
- 10,000 rows imported with a clean report; re-running produces zero changes.
- usage_daily for a test month reconciles to a Meta statement fixture within 1%.
- The reimbursement ledger shows the correct AED figure for Aquamena service months 1–3.
```

### Phase 6 — Hardening (3 days)

```
PHASE 6 — Make it survivable.

- scripts/backup.py: nightly pg_dump, age-encrypted, pushed to Backblaze B2 or Cloudflare R2, 30-day
  retention. Plus a restore script, and a test that actually restores into a clean container and
  verifies row counts. An untested backup is not a backup.
- Alerting to your own WhatsApp number via the platform: webhook 5xx rate, ARQ queue depth > 500,
  LLM failover triggered, Meta quality rating change, tenant at 80% of message cap, disk > 80%,
  backup failed.
- Netdata or Uptime Kuma on status.hmhagents.com behind basic auth; external UptimeRobot check on
  /health for independent uptime evidence against the contracted 99.5%.
- Load test with locust: 200 concurrent conversations, LLM stubbed at realistic latency. Record p50
  and p95 and the resource ceiling. Document the real tenant capacity of the box.
- Graceful degradation: both LLM providers down → holding message and queue, never a crash. Postgres
  down → webhook still returns 200 and buffers to Redis.
- RUNBOOK.md: deploy, rollback, restore, rotate a Meta token, onboard a tenant, Meta quality-rating
  recovery, what to do when a client says "the agent said something wrong".
- SECURITY.md and a short DPA-ready data-processing description per tenant.

Acceptance:
- Restore from an encrypted backup into a clean container succeeds and row counts match.
- 200 concurrent conversations sustained with p95 under 3 seconds (LLM stubbed).
- Killing the Postgres container does not lose an inbound message.
```

---

## Section C — sequencing against the Aquamena commitment

The contract promises Aquamena a 15-working-day build from 15 September with go-live 5 October. The
phases above total 24 days because they also build HMH Labz's platform foundation, which is your
investment and not Aquamena's deliverable.

**To protect 5 October**, build in this order:

1. Phase 0, 1, 2 — the agent working on Aquamena's number *(10 days)*
2. Phase 3 — the dashboard they were promised *(4 days)*
3. Phase 4, partial — templates, one segment, manual campaign send *(2 days)*
4. **Go live 5 October**
5. Phase 5, 6 and the rest of Phase 4 during service months 1–3, which are free anyway and therefore
   the right window for finishing the platform while the first client is live and forgiving.

That sequencing is also why the free months are commercially sensible rather than just generous: you
are using them to finish the product.

---

## Section D — what to get ready before any code is written

These block the build and none of them are technical:

1. **Meta Business Manager verified** for HMH Labz, and Tech Provider status applied for. This can
   take days to weeks and is the single most likely cause of a missed go-live date. Start it first.
2. **Aquamena's phone number** confirmed and *not* registered on the consumer WhatsApp app.
3. **Domain** — `hmhagents.com` or similar, on Cloudflare, with a wildcard DNS record.
4. **API keys** — Google AI Studio and OpenRouter, each with a spend cap set at the provider.
5. **Aquamena's Excel file**, the real one, messy.
6. **Confirmed coupon structure** — particularly the AED 225 book and the AED 110 Ajman offer, which
   the contract already flags as needing confirmation before any agent quotes a price.
7. **The escalation phone number** for complaints, and who is actually watching it.
