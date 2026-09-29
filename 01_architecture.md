# HMH Labz — Multi-Tenant WhatsApp AI Agent Platform
## Technical Architecture for Hostinger VPS KVM 4

**Version** 1.0 · **Date** 28 September 2026 · **Author** HMH Labz LLP
**First tenant** Aquamena Water Treatment L.L.C · go-live 5 October 2026

---

## 1. Decision summary

| Question | Decision | Rationale |
|---|---|---|
| Language | **Python 3.12** (FastAPI) | LLM SDKs, Pydantic validation of tool args, pandas for Excel ingest, Arabic text handling |
| Orchestration | **Docker Compose** | Kubernetes control plane would consume ~30% of a 4 vCPU box |
| Database | **PostgreSQL 16 + pgvector** | One instance, `tenant_id` everywhere, Row-Level Security in the DB |
| Queue / state | **Redis 7** | Conversation state, webhook dedup, rate limits, ARQ broker |
| Workers | **ARQ** | Async-native, Redis-backed, lighter than Celery |
| Edge | **Caddy 2** | Automatic TLS, wildcard subdomains, ~50 MB |
| Dashboard | **React 18 + Vite**, static build | Zero runtime CPU; built in CI, served as files |
| Primary LLM | **Gemini 3 Flash** (direct Google AI API) | Cost, latency, native Arabic, reliable tool calling |
| Classifier LLM | **Gemini 2.5 Flash-Lite** | $0.10/$0.40 per M tokens |
| Failover LLM | **OpenRouter** | OpenAI-compatible, one key, many models |
| Embeddings | **fastembed (ONNX, in-process)** | ~50 ms on CPU, no API cost, no daemon |
| Local inference | **Not used** | See §9 |

---

## 2. Target hardware

**Hostinger VPS KVM 4** — 4 vCPU · 16 GB RAM · 200 GB NVMe · 16 TB bandwidth
OS: Ubuntu 24.04 LTS · Region: closest to UAE (Hostinger's Middle East / EU-central)

### 2.1 Memory budget

| Service | Allocation | Notes |
|---|---|---|
| PostgreSQL 16 | 3.0 GB | `shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=32MB`, `max_connections=100` |
| Redis 7 | 0.5 GB | `maxmemory 512mb`, `maxmemory-policy allkeys-lru` |
| FastAPI (2 uvicorn workers) | 0.6 GB | ~300 MB per worker |
| ARQ workers (3) | 1.0 GB | Campaign sends, LLM calls, Excel imports |
| Caddy | 0.05 GB | |
| Monitoring (Netdata or Uptime Kuma) | 0.35 GB | |
| **Total committed** | **≈ 5.5 GB** | |
| **Free for page cache and burst** | **≈ 10.5 GB** | |

### 2.2 Why 4 vCPU is enough

Agent work is **I/O-bound, not CPU-bound**. Every conversation turn is time spent waiting on the
Gemini API and the Meta Graph API over the network. Async Python holds thousands of concurrent
in-flight requests on four cores because they are almost all idle, waiting.

**Capacity estimate:** Aquamena runs roughly 2,000 messages/day (≈120 orders + support + campaign
replies). One box of this size comfortably supports **15–25 tenants of that scale.**

**First bottleneck when you outgrow it:** PostgreSQL, not the application. Move the database to its
own KVM instance (or Hostinger managed Postgres) before you scale the app tier. Second bottleneck is
outbound campaign throughput, which is governed by Meta's rate limits, not your CPU.

---

## 3. Service topology

```
                         Internet
                            │
                            ▼
              ┌──────────────────────────────┐
              │   Caddy 2  (TLS, :80/:443)   │
              │  api.hmhagents.com           │
              │  *.hmhagents.com  (tenants)  │
              └──────┬──────────────┬────────┘
                     │              │
        static files │              │ reverse proxy
                     ▼              ▼
            ┌────────────────┐  ┌──────────────────────────┐
            │ dashboard/dist │  │  FastAPI  (uvicorn × 2)  │
            │ React SPA      │  │  /webhook/meta           │
            └────────────────┘  │  /api/v1/*               │
                                └────┬────────────┬────────┘
                                     │            │
                          enqueue    │            │  read/write
                                     ▼            ▼
                            ┌──────────────┐  ┌──────────────────┐
                            │   Redis 7    │  │  PostgreSQL 16   │
                            │ queue/state  │  │  + pgvector      │
                            └──────┬───────┘  │  RLS per tenant  │
                                   │          └──────────────────┘
                          consume  ▼                   ▲
                       ┌────────────────────────┐      │
                       │  ARQ workers × 3       │──────┘
                       │  · agent turns         │
                       │  · campaign sends      │
                       │  · Excel imports       │
                       │  · scheduled jobs      │
                       └───────┬────────────────┘
                               │
              ┌────────────────┼─────────────────┐
              ▼                ▼                 ▼
      Google AI API      OpenRouter        Meta Graph API
      (Gemini 3 Flash)   (failover)        (send messages)
```

### 3.1 docker-compose services

| Container | Image | CPU limit | Mem limit |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | 0.25 | 128 M |
| `api` | built from `./api` | 1.5 | 1 G |
| `worker` | same image, ARQ entrypoint | 1.5 | 1.5 G |
| `scheduler` | same image, ARQ cron | 0.25 | 384 M |
| `postgres` | `pgvector/pgvector:pg16` | 1.5 | 3.5 G |
| `redis` | `redis:7-alpine` | 0.5 | 640 M |

Limits are ceilings, not reservations — they exist to stop one runaway service taking the box down.
Total CPU limits deliberately exceed 4.0 because services are never all busy at once.

---

## 4. Multi-tenancy — the part that matters most

### 4.1 Meta sends every webhook to ONE URL

This is the single architectural fact that determines the whole design. Your Meta App has **one**
webhook callback URL. Every message for every client arrives there. Routing is your job:

```
POST /webhook/meta
  entry[0].id                                  → WABA ID
  entry[0].changes[0].value.metadata
           .phone_number_id                    → THE TENANT KEY
```

Look up `phone_number_id` in `tenant_channels` → get `tenant_id` → set the Postgres session
variable → every subsequent query is automatically scoped by RLS.

**If this lookup is wrong, Client A's customers receive Client B's answers.** It is the highest-risk
line of code in the platform and must have its own test suite.

### 4.2 Meta Tech Provider model

Register HMH Labz as a **Meta Tech Provider** and onboard each client through **Embedded Signup**:

- One Meta App, one webhook, one set of credentials you maintain.
- Each client keeps **their own WABA and their own billing** — which is exactly what the Aquamena
  contract states at clause 8, and what the reimbursement arrangement for months 1–3 relies on.
- Client grants your App permission on their WABA; you store the resulting system-user token.

**Do not build one Meta App per client.** It does not scale past about three, and every client then
needs their own app review.

### 4.3 Isolation strategy

Single database, single schema, `tenant_id UUID NOT NULL` on every tenant-scoped table, enforced by
**PostgreSQL Row-Level Security**:

```sql
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON conversations
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

The application sets `SET LOCAL app.tenant_id = '<uuid>'` at the start of every transaction. A
missing or wrong tenant context returns **zero rows**, not another tenant's rows. This is
defence-in-depth: an ORM bug or a forgotten `WHERE` clause cannot leak across tenants because the
database itself refuses.

Connect the application as a **non-superuser role** — RLS is bypassed by superusers and by table
owners, so the app role must own nothing.

---

## 5. Data model

### 5.1 Platform tables (no tenant_id — these define tenancy)

```sql
tenants
  id uuid pk, name text, legal_name text, slug text unique,
  status text check (status in ('trial','active','suspended','churned')),
  timezone text default 'Asia/Dubai', locale_default text default 'en',
  contract_ref text,              -- 'HMH-AQ-2026-CSA-01'
  service_start date, free_months_until date,
  meta_charges_borne_by_us_until date,   -- 2026-12-14 for Aquamena
  created_at timestamptz

tenant_channels                   -- the webhook routing table
  id uuid pk, tenant_id uuid fk,
  waba_id text, phone_number_id text unique not null,
  display_phone text,
  access_token_encrypted bytea,   -- Fernet, key from env, NEVER plaintext
  token_expires_at timestamptz,
  webhook_verify_token text,
  quality_rating text, messaging_limit_tier text,
  is_active bool default true

tenant_settings
  tenant_id uuid pk fk,
  llm_provider text default 'gemini',        -- 'gemini' | 'openrouter'
  llm_model_chat text default 'gemini-3-flash',
  llm_model_classify text default 'gemini-2.5-flash-lite',
  monthly_message_cap_aed numeric(10,2),
  monthly_token_cap int,
  business_hours jsonb, escalation_phone text,
  agent_persona jsonb,            -- name, tone, languages
  feature_flags jsonb

platform_users                    -- HMH Labz staff
  id uuid pk, email text unique, password_hash text,
  role text check (role in ('owner','ops','support')), totp_secret text

tenant_users                      -- client-side logins (Zameer, his team)
  id uuid pk, tenant_id uuid fk, email text, password_hash text,
  role text check (role in ('admin','agent','viewer')),
  unique (tenant_id, email)
```

### 5.2 Tenant-scoped tables (all RLS-protected)

```sql
customers
  id uuid pk, tenant_id uuid, wa_id text,            -- E.164, no '+'
  name text, area text, emirate text, language text,
  source text,                     -- 'excel_import' | 'qr_van' | 'referral' | 'inbound'
  opt_in_status text check (opt_in_status in ('pending','opted_in','opted_out')),
  opt_in_at timestamptz, opt_in_evidence jsonb,      -- PDPL/TDRA proof
  opt_out_at timestamptz,
  lifetime_orders int default 0, last_order_at timestamptz,
  external_ref text,               -- their Excel row id
  unique (tenant_id, wa_id)

coupon_books
  id uuid pk, tenant_id uuid, customer_id uuid,
  sku text, price_aed numeric, bottles_total int, bottles_free int,
  bottles_remaining int, purchased_at timestamptz, expires_at date

orders
  id uuid pk, tenant_id uuid, customer_id uuid,
  order_no text,                   -- human reference, per-tenant sequence
  status text check (status in ('draft','confirmed','out_for_delivery','delivered','cancelled')),
  items jsonb,                     -- [{sku, name, qty, unit_price_aed}]
  total_aed numeric, source text,  -- 'agent' | 'dashboard' | 'manual'
  area text, delivery_slot text, delivery_date date,
  notes text, created_by text, created_at timestamptz

products
  id uuid pk, tenant_id uuid, sku text, name_en text, name_ar text,
  category text,                   -- 'water' | 'snack'
  brand text, price_aed numeric, is_active bool,
  cross_sell_priority int, stock_note text

conversations
  id uuid pk, tenant_id uuid, customer_id uuid,
  channel_id uuid fk tenant_channels,
  state text,                      -- 'open' | 'awaiting_human' | 'closed'
  assigned_to uuid null,           -- tenant_users.id when escalated
  last_inbound_at timestamptz, last_outbound_at timestamptz,
  service_window_expires_at timestamptz,   -- inbound + 24h
  summary text,                    -- rolling summary, keeps prompts small
  language text

messages
  id uuid pk, tenant_id uuid, conversation_id uuid,
  wamid text unique,               -- Meta's message id; the dedup key
  direction text check (direction in ('in','out')),
  msg_type text,                   -- text|image|audio|interactive|template
  body text, media_url text, transcript text,
  template_name text, pricing_category text,   -- marketing|utility|service|auth
  cost_aed numeric(8,5),           -- stamped at send time
  status text,                     -- sent|delivered|read|failed
  error_code text, error_detail text,
  llm_model text, prompt_tokens int, completion_tokens int,
  latency_ms int, created_at timestamptz

message_templates
  id uuid pk, tenant_id uuid, name text, language text,
  category text, meta_status text,  -- APPROVED|PENDING|REJECTED
  body text, variables jsonb, meta_template_id text

campaigns
  id uuid pk, tenant_id uuid, name text, template_id uuid,
  segment_query jsonb,             -- declarative segment definition
  status text check (status in ('draft','approved','sending','paused','done','cancelled')),
  approved_by uuid, approved_at timestamptz,
  scheduled_for timestamptz, throttle_per_minute int default 60,
  budget_cap_aed numeric,
  sent_count int, delivered_count int, read_count int,
  reply_count int, order_count int, spend_aed numeric

campaign_recipients
  id uuid pk, tenant_id uuid, campaign_id uuid, customer_id uuid,
  status text, wamid text, sent_at timestamptz, cost_aed numeric

knowledge_chunks
  id uuid pk, tenant_id uuid, source text, title text,
  content text, embedding vector(384),   -- fastembed bge-small-en-v1.5
  language text
  -- index: ivfflat (embedding vector_cosine_ops)

usage_daily                        -- per-tenant metering, the billing spine
  tenant_id uuid, day date,
  msgs_in int, msgs_out int,
  marketing_count int, utility_count int, service_count int,
  meta_cost_aed numeric(10,4),
  llm_prompt_tokens bigint, llm_completion_tokens bigint,
  llm_cost_usd numeric(10,5),
  primary key (tenant_id, day)

audit_log
  id bigserial pk, tenant_id uuid, actor text, action text,
  entity text, entity_id uuid, before jsonb, after jsonb, at timestamptz
```

### 5.3 Why `usage_daily` matters commercially

This table is how you know, per client per day, exactly what Meta cost and what the LLM cost. For
Aquamena specifically it is how you evidence the reimbursement of message charges for service months
1–3 under clause 8, and how you enforce the AED 1,500 monthly ceiling. Build it in phase 1, not
later — retrofitting metering means you cannot bill accurately for the months you missed.

---

## 6. Request lifecycle

### 6.1 Inbound message

```
1.  Meta POSTs /webhook/meta
2.  Verify X-Hub-Signature-256 (HMAC-SHA256, app secret) — reject 403 if bad
3.  Extract phone_number_id → resolve tenant (cached in Redis, 5 min TTL)
4.  Dedup: SETNX wamid in Redis, 48h TTL. Already seen → 200 OK, drop.
5.  Persist raw payload, enqueue ARQ job, RETURN 200 WITHIN 200 ms
        ── Meta retries anything slower or non-2xx. Never do LLM work here. ──
6.  Worker: SET LOCAL app.tenant_id
7.  Voice note? download media → transcribe → store transcript
8.  Classify intent (Flash-Lite, ~120 tokens): order | balance | delivery |
    complaint | price | optout | smalltalk | unknown
9.  optout  → mark opted_out, send confirmation, STOP (TDRA requirement)
    complaint / refund / dispute → escalate to human, notify, STOP
10. Build context: customer + coupon balance + last 10 turns + rolling summary
    + RAG over knowledge_chunks (top 4)
11. Gemini 3 Flash with tool schemas (see 02_agent_prompts.md)
12. Execute tool calls against Postgres inside one transaction
13. Validate the reply: no invented prices, no invented stock, has an answer
14. Send via Meta Graph API, stamp cost_aed, record tokens and latency
15. Update conversation summary every 6 turns (cheap model)
```

### 6.2 Outbound campaign

```
1.  Operator builds campaign in dashboard, picks APPROVED template + segment
2.  Segment resolves to recipients — opted_in ONLY, never 'pending'
3.  Dry-run preview: recipient count, estimated AED cost, sample rendered message
4.  Human approval required (approved_by, approved_at) — no auto-send, ever
5.  Scheduler fans out at throttle_per_minute, respecting Meta's tier limit
6.  Each send checks the live budget cap before firing; cap hit → pause + alert
7.  Delivery/read/reply webhooks update campaign counters
8.  Replies route into the normal inbound pipeline as service messages
```

---

## 7. LLM router

```python
# api/llm/router.py — the whole failover story in one class
PROVIDERS = {
    "gemini":     dict(base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                       key_env="GOOGLE_AI_API_KEY"),
    "openrouter": dict(base_url="https://openrouter.ai/api/v1",
                       key_env="OPENROUTER_API_KEY"),
}

# Per-tenant model choice read from tenant_settings, so a client can be moved
# to a stronger model with an UPDATE, not a deploy.

# Failover: primary → 2 retries with exponential backoff (0.5s, 2s)
#           on 429 / 5xx / timeout → OpenRouter with the equivalent model
#           both down → queue the turn, send a holding message, alert ops
```

Both providers speak the OpenAI Chat Completions shape, so one client library serves both. Do **not**
run the LiteLLM proxy as a container — it is another service to supervise and another failure mode on
a box this size. A thin router class in-process is ~120 lines and you can debug it.

### 7.1 Token discipline

- System prompt hard-capped at **1,500 tokens**. Measure it in CI and fail the build if it grows.
- Conversation window: last **10 turns** plus a rolling summary, never the full history.
- RAG: top **4** chunks, 200 tokens each maximum.
- Cache the system prompt where the provider supports it.
- Classification runs on Flash-Lite at a fraction of the cost and short-circuits the expensive path
  for opt-outs and smalltalk — roughly 30% of traffic never reaches the main model.

---

## 8. Dashboard

**React 18 + Vite + TypeScript**, built to static assets in CI, served by Caddy. Zero runtime CPU on
the VPS. Recommended: TanStack Query for data, Tailwind + shadcn/ui for components, Recharts for the
two or three charts that earn their place.

### 8.1 Two audiences, one codebase

| Surface | Host | Who | Sees |
|---|---|---|---|
| Tenant dashboard | `aquamena.hmhagents.com` | Zameer and his team | Their own data only |
| Platform console | `admin.hmhagents.com` | HMH Labz | All tenants, usage, billing, health |

Same bundle, role-gated routes, tenant resolved from the JWT — never from the URL.

### 8.2 Tenant dashboard screens (Aquamena's contracted scope)

1. **Today** — orders today, live conversations, awaiting-human count, message spend this month
   against cap, agent health
2. **Orders** — table, filter by area/status/date, open one, change status, print delivery list.
   *This is the screen that replaces their Excel.*
3. **Conversations** — live inbox, read any thread, take over from the agent, hand back
4. **Customers** — search, coupon balance, order history, opt-in status and evidence
5. **Campaigns** — build, preview with cost estimate, approve, watch results
6. **Costs** — message spend by day and category, against cap. For months 1–3 this shows
   *"borne by HMH Labz"* rather than an amount due.
7. **Settings** — business hours, escalation number, products and prices, team logins

### 8.3 Live updates

Server-Sent Events on `/api/v1/stream` — one long-lived GET per open dashboard, far lighter than
WebSockets and it survives Caddy's default config without extra work. Fall back to 15-second polling.

---

## 9. Local inference (Ollama) — assessed and rejected

**Verdict: do not run Ollama for conversation on this box.**

| Factor | Reality on 4 shared vCPU, no GPU |
|---|---|
| Throughput | 7–8B model at Q4 → roughly **3–8 tokens/sec** |
| Latency | A 200-token WhatsApp reply takes **30–60 seconds** |
| CPU contention | Generation pegs all 4 cores, starving the API for every other tenant |
| Arabic quality | Materially worse than Gemini on small models |
| Tool calling | Unreliable below ~7B — it will invent order quantities and prices |
| Cost saving | None worth having: Gemini 2.5 Flash-Lite is $0.10 / $0.40 per M tokens |

A customer asking "how many bottles do I have left" will not wait 45 seconds. And the failure is not
graceful — it degrades every other client on the box simultaneously.

**The one legitimate local model is embeddings.** Use `fastembed` with `bge-small-en-v1.5` (384
dimensions, ~130 MB, ONNX) **in-process** — about 50 ms per chunk on CPU, no API cost, no daemon to
supervise. This is strictly better than running Ollama for the same job.

**Revisit local inference when:** you rent a GPU instance, or a client's data-residency terms
prohibit sending text to a US API. Neither applies today. If a UAE client ever does require
residency, the answer is Gemini via a UAE/EU region endpoint or an Azure OpenAI UAE-North
deployment — not CPU inference.

---

## 10. Security

### 10.1 Non-negotiable

1. **Verify `X-Hub-Signature-256`** on every webhook. An unverified endpoint lets anyone inject
   messages as any customer.
2. **Encrypt Meta access tokens at rest** — application-level Fernet with the key in environment
   (later: a secrets manager). Disk encryption alone does not protect against a Postgres dump.
3. **Postgres RLS** as described in §4.3, app connecting as a non-owner, non-superuser role.
4. **Never log message bodies or tokens.** Log `wamid`, tenant, intent, latency, cost. UAE PDPL
   applies to the customer data you hold.
5. **Offsite backups.** `pg_dump` nightly, encrypted with age/gpg, pushed to Backblaze B2 or
   Cloudflare R2. Hostinger snapshots live on Hostinger — that is redundancy, not a backup. Test a
   restore before go-live and again monthly.
6. **UFW**: allow 22 (key-only, non-standard port), 80, 443. Nothing else. Postgres and Redis bind to
   the Docker network only, never `0.0.0.0`.
7. **fail2ban** on SSH, Caddy rate limits on `/api/v1/auth/*`.
8. **Automatic security updates** (`unattended-upgrades`), weekly reboot window if a kernel update
   needs it.

### 10.2 Compliance carried from the Aquamena contract

- **Opt-in evidence** stored per customer (`opt_in_evidence` jsonb: source, timestamp, wording
  shown). TDRA's Unsolicited Electronic Communications Policy requires you to be able to prove it.
- **STOP handling is absolute** — it short-circuits before any LLM call and can never be overridden
  by a campaign.
- **AI disclosure** in the first message of any new conversation.
- **Data residency and retention** documented per tenant; media files purged after 90 days unless
  attached to an open dispute.

---

## 11. Deployment

### 11.1 First-time provisioning (once)

```bash
# 1. Harden
adduser deploy && usermod -aG sudo deploy
# SSH keys only, PasswordAuthentication no, Port 2222
ufw default deny incoming && ufw allow 2222/tcp && ufw allow 80,443/tcp && ufw enable
apt install -y fail2ban unattended-upgrades

# 2. Docker
curl -fsSL https://get.docker.com | sh && usermod -aG docker deploy

# 3. Swap — 4 GB, protects against OOM on an import spike
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10

# 4. Kernel tuning for many idle sockets
echo 'net.core.somaxconn=4096'          >> /etc/sysctl.d/99-agents.conf
echo 'net.ipv4.tcp_max_syn_backlog=4096'>> /etc/sysctl.d/99-agents.conf
echo 'fs.file-max=200000'               >> /etc/sysctl.d/99-agents.conf

# 5. Clone, configure, start
git clone <repo> /opt/agents && cd /opt/agents
cp .env.example .env    # fill secrets
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed_tenant --slug aquamena
```

### 11.2 Ongoing deploys

GitHub Actions on push to `main`: run tests → build images → push to GHCR → SSH → `docker compose
pull && up -d` → `alembic upgrade head` → smoke-test `/health`. Rollback is
`docker compose up -d --force-recreate` against the previous tag.

### 11.3 Monitoring and alerts

- **Netdata** (or Uptime Kuma if you want it lighter) on `status.hmhagents.com`, basic-auth'd.
- **Alerts to your WhatsApp** via the platform's own number — pleasing and practical:
  webhook 5xx rate, ARQ queue depth > 500, LLM failover triggered, Meta quality rating dropped,
  tenant approaching message cap, disk > 80%, failed backup.
- **Uptime evidence.** The Aquamena contract commits to 99.5% monthly on HMH-controlled components.
  Record it: an external check (UptimeRobot free tier) against `/health` every minute gives you an
  independent number you can show the client rather than one from your own box.

---

## 12. Build phases

| Phase | Scope | Days | Done when |
|---|---|---|---|
| **0** | VPS provisioned, Docker, Caddy, TLS, Postgres+RLS, Redis, CI, `/health` | 2 | `curl https://api.../health` returns 200 over valid TLS |
| **1** | Webhook ingest, signature verification, dedup, tenant routing, message persistence | 3 | A real WhatsApp message lands in `messages` with the right `tenant_id`; a wrong signature is rejected |
| **2** | Support Agent: classifier, tools, RAG, order + balance + delivery flows, escalation | 5 | 30 scripted conversations pass, including 6 Arabic and 3 escalations |
| **3** | Dashboard: Today, Orders, Conversations, Customers | 4 | Zameer can take an order end-to-end without touching Excel |
| **4** | Outreach Agent: templates, segments, campaigns, approval gate, budget cap | 4 | A 50-recipient campaign sends, costs are metered, cap pauses it |
| **5** | Excel import, opt-in capture, QR landing, metering, costs screen | 3 | 10,000 rows imported with a dedup report; `usage_daily` reconciles to Meta's statement |
| **6** | Hardening: backups + restore test, alerts, load test, runbook | 3 | Restore from backup into a clean container succeeds; 200 concurrent conversations sustained |

**24 working days total** — against 15 contracted for Aquamena's build. The difference is the
multi-tenant foundation and the platform console, which are HMH Labz's own investment, not
Aquamena's deliverable. Phases 0–3 plus the campaign basics of phase 4 cover the contracted scope;
sequence those first and keep 5 October safe.

---

## 13. Running cost per month

| Item | USD | AED |
|---|---|---|
| Hostinger KVM 4 (renewal rate) | ~25 | ~92 |
| Domain + Cloudflare (free tier) | ~1 | ~4 |
| Backup storage (B2, 50 GB) | ~1 | ~4 |
| Gemini API — 1 tenant at Aquamena volume | ~8–15 | ~30–55 |
| **Platform total, first tenant** | **~35–42** | **~130–155** |
| Each additional tenant (LLM only) | ~8–15 | ~30–55 |

Against AED 2,500/month per tenant, gross margin is roughly **94%** once the free period ends, and
the box carries 15+ tenants before you spend another dirham on infrastructure. Note that the AED 400
of monthly infrastructure quoted to Aquamena as "borne by HMH Labz" during months 1–3 is comfortably
above true cost — the real figure is nearer AED 150.
