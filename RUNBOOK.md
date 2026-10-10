# RUNBOOK — HMH Labz agent platform

What to do, step by step, when operating the production box. Commands run from `/opt/agents` as
the `deploy` user unless stated. `dc` below means `docker compose -f docker-compose.yml`.

- [First-time setup additions](#first-time-setup-additions)
- [Deploy](#deploy) · [Rollback](#rollback)
- [Backups and restore](#backups-and-restore)
- [Rotate a Meta token](#rotate-a-meta-token)
- [Onboard a tenant](#onboard-a-tenant)
- [Alerts: what each one means](#alerts-what-each-one-means)
- [Meta quality-rating recovery](#meta-quality-rating-recovery)
- ["The agent said something wrong"](#the-agent-said-something-wrong)
- [When a dependency is down](#when-a-dependency-is-down)
- [Capacity and the load test](#capacity-and-the-load-test)

---

## First-time setup additions

Provision the box as in `01_architecture.md` §11.1 (hardening, Docker, swap, sysctl). Then:

1. **`.env`** from `.env.example`. Besides the Phase 0–5 values, set:
   - `DB_BACKUP_USER` / `DB_BACKUP_PASSWORD` — the backup role (created by the Postgres init
     script on first start: read-only, BYPASSRLS).
   - `BACKUP_AGE_RECIPIENT`, `BACKUP_BUCKET`, `BACKUP_ENDPOINT_URL`, `BACKUP_ACCESS_KEY_ID`,
     `BACKUP_SECRET_ACCESS_KEY` — see [Backups](#backups-and-restore).
   - `ALERT_TENANT_SLUG` (HMH Labz's own tenant, whose WhatsApp number sends alerts) and
     `ALERT_TO` (your phone, E.164, comma-separated for several).
   - `STATUS_BASIC_AUTH_HASH` — `docker run --rm caddy caddy hash-password`.
   - `OPTIN_BASE_URL=https://go.<domain>` (QR codes point here; set it before printing any).
2. **DNS** (Cloudflare, grey cloud): `api.`, `admin.`, `go.`, `status.` and the wildcard `*.`.
3. **Alert template.** In HMH Labz's own WhatsApp Business Account create a UTILITY template
   `platform_alert`, English:
   `HMH Labz platform alert: {{1}} Check the console for details.`
   (Meta rejects a template that starts or ends with a variable.) Until it is approved, alerts
   are only logged at CRITICAL.
4. **Staff login for the console:**
   `dc run --rm api python -m api.scripts.platform_users add --email you@hmhlabz.com --role owner`
   — scan the QR it prints with an authenticator app. It is shown once.
5. **Status page:** open `https://status.<domain>`, create the Uptime Kuma (v2) admin, add monitors:
   `http://api:8000/health` (every 60 s), `https://<tenant>.<domain>/` and
   `https://go.<domain>/q/x` (expects 404 — proves the page host is up).
6. **External uptime evidence:** UptimeRobot (free) HTTP(S) monitor on
   `https://api.<domain>/health`, 1-minute interval, alert contact = your email. This is the
   independent number for the contract's 99.5%.

## Deploy

```bash
deploy/deploy.sh            # origin/main
deploy/deploy.sh <sha>      # a specific commit
```

It fetches, builds images tagged with the commit, builds the dashboard into `dist.new`, takes a
pre-deploy backup (when backups are configured), runs `alembic upgrade head`, restarts, and waits
for `/health`. If anything fails before the restart, the running version keeps serving.
`deploy/history.log` records each successful deploy.

After a deploy: open a tenant dashboard and the console; send a WhatsApp message to the test
number and see it answered.

## Rollback

```bash
tail -3 deploy/history.log     # the previous good sha
deploy/deploy.sh <previous sha>
```

The schema is **not** downgraded. Old code runs on a schema that only gained tables or columns
(every migration so far, 0001–0008). If a release's migration dropped or renamed something, its
commit message says so and how to roll back; in that case restore is the fallback (below).

## Backups and restore

- **Nightly** at 01:30 UTC (scheduler): `pg_dump -Fc` of a consistent snapshot as the backup
  role, encrypted with `age` to `BACKUP_AGE_RECIPIENT`, uploaded with a manifest of every table's
  row count, objects older than 30 days deleted. Success is stamped in Redis; a failure, or no
  success for 26 hours, alerts.
- **Keys.** Generate once on your own machine, not the server:
  `uv run python -m api.scripts.backup keygen --out backup-age-key.txt`. Put the printed public
  key in `BACKUP_AGE_RECIPIENT`. Keep the private key file in the password manager plus one
  offline copy. Without it, no backup can be read — that is the point, and the risk.
- **By hand:** `dc run --rm scheduler python -m api.scripts.backup run` · `… backup list`.

**Restore** (also the monthly drill — do it on the first Monday, into a scratch database):

```bash
# copy the private key to the box only for the restore, then delete it
dc run --rm -v "$PWD/backup-age-key.txt:/key.txt:ro" scheduler \
  python -m api.scripts.backup restore --identity-file /key.txt \
  --target-db agents_restore_$(date +%Y%m%d) \
  --admin-url "postgresql://postgres:$POSTGRES_SUPERUSER_PASSWORD@postgres:5432/postgres"
shred -u backup-age-key.txt
```

It creates a **new** database (it refuses an existing one), restores, and compares every table's
row count with the backup's manifest — exit code 0 and "Row counts match" means good.
To put a restored database into service: stop `api worker scheduler`, rename databases in psql
(`ALTER DATABASE agents RENAME TO agents_broken; ALTER DATABASE agents_restore_… RENAME TO agents;`),
start them again. Webhooks that arrive meanwhile wait in Redis (see below).

## Upgrade Uptime Kuma

The image is pinned in `docker-compose.yml`. Patch releases within 2.x: change the tag, deploy.
A release that migrates the database (as v1 → v2 did) rewrites the SQLite file in the
`uptimekuma` volume on first start, so copy the volume first; Kuma's JSON export is gone in v2
and this copy is the only backup:

```bash
dc stop uptime-kuma
mkdir -p ~/backups
docker run --rm -v hmh-agents_uptimekuma:/data:ro -v ~/backups:/backup alpine \
  tar czf /backup/uptimekuma-$(date -u +%Y%m%dT%H%M%SZ).tgz -C /data .
deploy/deploy.sh <sha>
dc logs -f uptime-kuma        # wait for the migration to finish; do NOT stop it midway
```

(The compose project is `hmh-agents`; `docker volume ls | grep uptimekuma` confirms the name.)
Rollback: `dc stop uptime-kuma`, empty the volume and untar the copy into it, then deploy the
previous sha. The older Kuma cannot run on a database the newer one has migrated.

## Rotate a Meta token

System-user tokens can be permanent or expire; the console and the tenant's Today screen warn
7 days before a recorded expiry.

```bash
dc run --rm api python -m api.scripts.channel_token list
dc run --rm api python -m api.scripts.channel_token set --phone-number-id <id> [--expires YYYY-MM-DD]
```

The token is typed at a hidden prompt, checked against Meta before it replaces the old one, and
stored encrypted. Then revoke the old token in Meta Business Manager.

## Onboard a tenant

1. Embedded Signup in Meta → note `phone_number_id`, `waba_id`, the system-user token.
2. Create the tenant, channel and first admin (`api.scripts.seed_tenant` for profiles defined
   there; the token comes from `SEED_META_ACCESS_TOKEN`), then rotate the token in with
   `channel_token set` if it was not passed.
3. Modules: `python -m api.scripts.modules enable --slug <slug> <preset>`
   (`water_delivery`, `real_estate`, `law_firm`).
4. Their customer list: `python -m api.scripts.import_excel --slug <slug> file.xlsx` (dry run,
   check the report), then again with `--commit`. Imported customers are *not* opted in.
5. In their dashboard (Settings → Opt-in QR codes) create the QR codes they will print.
6. Set `monthly_fee_aed`, `service_start`, `free_months_until`, `meta_charges_borne_by_us_until`
   on the tenant row (console margin and reimbursement ledger read them).
7. `python -m api.scripts.dpa --slug <slug> > dpa-<slug>.md` — review, send with the contract.

## Alerts: what each one means

| Alert | Means | Do |
|---|---|---|
| Webhook errors (5xx) | > 5% of ≥ 10 webhooks in 5 min failed | `dc logs --tail 200 api`; usually Redis or a bug. Meta retries failed webhooks for days. |
| Postgres unreachable — webhooks parked | DB down; messages wait in Redis | Fix Postgres (`dc ps`, `dc logs postgres`, disk). They replay by themselves within 30 s of recovery. |
| Job queue depth > 500 | workers behind | `dc ps worker`; `dc logs worker`; scale: `dc up -d --scale worker=2`. |
| LLM failover | Gemini failing, OpenRouter answering | Check Google AI status / key / quota. Nothing for customers to notice. |
| Both LLM providers failed | customers get the holding message; turns retry, then hand over | Check both providers' keys and spend caps. |
| Quality rating changed | Meta moved a number's rating | YELLOW pauses marketing, RED cancels it — see next section. |
| Tenant at 80% of cap | a tenant's message spend nears its cap | Tell the client; raise the cap only with their written OK. |
| Disk > 80% | the volume disk is filling | `docker system df`; prune old images: `docker image prune -a --filter until=168h`. |
| Backup failed / none in 26 h | last night's backup did not complete | Run it by hand (above) and read the error; check bucket keys and disk. |

## Meta quality-rating recovery

1. The platform already paused (YELLOW) or cancelled (RED) that tenant's campaigns. Leave them so.
2. In the console, look at the tenant's last campaigns: template, audience size, reply/opt-out
   counts. The usual cause is a broad or stale audience, or a template that reads as spam.
3. Fix the cause: narrower segment (recent buyers), fewer messages (raise the frequency cap
   days), clearer opt-out line in the template, better timing.
4. Wait for the rating to return to GREEN (Meta re-rates on its own over ~7 days of good
   sending). Customer-service replies are unaffected meanwhile.
5. Resume one small campaign, watch the next rating update, then return to normal.

## "The agent said something wrong"

1. Get the conversation (dashboard → Chats, or the customer's number). Note the time.
2. Take the conversation over in the dashboard and correct it with the customer **first**.
3. Find the turn: the outbound message row has the model, prompt version and token counts;
   `dc logs worker --since <time>` shows `turn_classified`, `support_prompt`, any
   `reply_rejected` with the failed check.
4. Classify the cause:
   - **Wrong data** (price, area, hours): fix it in the dashboard (Settings / Products); the
     agent reads tools, so the next answer is right.
   - **Missing knowledge**: if it is a product, price, area or hours, add it in the dashboard.
     Otherwise it belongs in the knowledge base — there is no tool to load articles yet (open
     item); note it and load it with the next engineering change.
   - **Model behaviour** (said something the prompt forbids): add the exact case to the
     evaluation scenarios in `api/tests/test_conversations.py`, fix the prompt or validator,
     run the live eval before deploying.
5. Tell the client what happened and what changed. Numbers in a reply always come from tools —
   if a wrong number was quoted, the tool data was wrong.

## When a dependency is down

- **Postgres down.** Webhooks still answer 200 and are parked in Redis (`webhook:buffer`); no
  message is lost (tested by killing the database connection mid-traffic). After 1 failure the
  webhook skips the database for 5 s. The scheduler replays the buffer every 30 s once Postgres
  is back. Dashboards show errors meanwhile. If Redis is *also* down the webhook answers 500 and
  Meta keeps retrying.
- **Redis down.** Webhooks still store messages (dedup falls back to the database), but jobs
  cannot be queued: the stranded-message sweeper re-queues them within 5 minutes of Redis
  returning. Redis keeps its data across restarts (AOF, fsync every second).
- **Both LLM providers down.** The customer gets one holding message; the turn retries, then
  hands over to a person with an urgent flag.
- **Meta API down.** A reply that cannot be sent fails its job (logged as an error); the message stays unanswered and the stranded-message sweeper
  queues it again once the failed job's record expires (about an hour). For a longer outage,
  have the team answer from the dashboard once Meta is back.

## Capacity and the load test

`loadtest/` drives real signed webhooks through the real API, queue and worker, with the LLM and
Meta replaced by `loadtest/stub_upstreams.py` (realistic latency). Each simulated customer waits
for its reply, then writes again after 8–20 s.

```bash
# on the box, against a staging copy (never production data):
uv run --group load python -m loadtest.seed
uv run uvicorn loadtest.stub_upstreams:app --port 9100 &
# run api + worker with GEMINI_BASE_URL=http://127.0.0.1:9100/llm/ and
# META_GRAPH_BASE_URL=http://127.0.0.1:9100/graph, then:
uv run --group load locust -f loadtest/locustfile.py --headless -u 200 -r 20 -t 5m --host http://127.0.0.1:8000
```

**Results, 2 October 2026** — measured on a development Mac (Apple silicon), not the VPS:
Postgres 16 local, real Redis 6, the API with 2 uvicorn workers, **one** worker process
(`WORKER_MAX_JOBS=50`). 200 concurrent conversations, ~11–12 turns/s sustained, 0 failures.

| Scenario | Webhook p50 / p95 | Turn p50 / p95 (webhook → reply sent) |
|---|---|---|
| LLM stubbed (instant), 2 s debounce | 11 ms / 74 ms | **2.3 s / 2.7 s** |
| LLM stubbed at realistic latency (classify ~0.35 s, reply ~1.1 s) | 12 ms / 99 ms | 3.8 s / 5.0 s |
| Platform alone (instant LLM, no debounce) | — | 0.59 s / 0.86 s |

The 2 s of every turn is the deliberate debounce (it waits for a second message typed right
after the first). Two changes came out of this test: the debounce now waits in the queue
(`_defer_by`) instead of asleep in a worker slot — before, it capped a worker at roughly
`max_jobs / 2.5` turns a second and pushed p95 to 6.7 s — and the worker polls every 0.1 s
instead of 0.5 s.

**Resource ceiling.** At ~11 turns/s: worker ≈ 1.6 CPU cores (mostly embedding each message for
knowledge search), Postgres ≈ 0.13 core, Redis and the API negligible. The worker container is
limited to 1.5 CPUs in `docker-compose.yml`, so **one worker ≈ 10 turns/s** on the box; add a
second with `dc up -d --scale worker=2` (the KVM 4 has 4 cores; Postgres needs ~1).
For scale: 10 turns/s is 36,000 customer messages an hour. A tenant whose busiest hour brings
1,000 messages uses under 3% of one worker. **Re-run the test on the VPS** before signing a
tenant expected to exceed a few thousand messages in an hour, and record the numbers here.
