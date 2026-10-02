# SECURITY — HMH Labz agent platform

How the platform protects tenants' data, where each control lives in the code, and what is not
covered yet. For the per-tenant data-processing description used in DPAs, run
`python -m api.scripts.dpa --slug <tenant>`.

## Tenant isolation (the control everything else rests on)

- **Row-Level Security in PostgreSQL, FORCED, on every tenant table.** Policy:
  `tenant_id = current_setting('app.tenant_id')`. The application connects as a role that owns
  nothing and cannot bypass RLS; the tenant is set per transaction with `set_config(…, true)`
  (parameterised, transaction-scoped). No tenant set → zero rows. (`api/db/session.py`)
- **Application guard on top:** an ORM query or write on a tenant table without a tenant context
  raises instead of returning an empty result; writing a row for another tenant raises.
- **Composite foreign keys** `(tenant_id, id)` so a row can never point at another tenant's row.
- **Tests:** `test_tenant_isolation.py` asserts every tenant table has forced RLS and a policy,
  and that no other table carries a `tenant_id`; `test_dashboard_isolation.py` has a
  cross-tenant case for **every** API route and fails if a new route has none.
- **Cross-tenant views** (the HMH console) open one tenant session per tenant. No database role
  used by the application can read all tenants at once. The backup role can (it must, to back
  up) and is read-only; its password is only in the server's `.env`.
- **Routing:** an inbound message's tenant is resolved from Meta's `phone_number_id` only.

## Inbound webhooks

- `X-Hub-Signature-256` (HMAC-SHA256 with the app secret) verified on the raw body before
  parsing; anything else gets 403. (`api/webhooks/signature.py`)
- The handler never calls a model or Meta; it stores and queues (target < 200 ms).
- Duplicates are dropped by wamid in Redis and again by a unique constraint in the database.
- If Postgres is down, verified bodies wait in Redis and are replayed; nothing unverified is
  ever buffered.

## Secrets

- All secrets come from the environment; none are in the repository.
- Meta access tokens are encrypted at rest (Fernet, key from `APP_ENCRYPTION_KEY`); only
  ciphertext is stored, and model `repr`s hide it.
- Nothing logs a token, a password, or a message body: database errors are logged by type only
  (SQLAlchemy `hide_parameters`), request logs carry the path without the query string.
- Tokens are entered at hidden prompts (`channel_token`, `platform_users`, `seed_tenant` via
  environment), never as command-line arguments.

## People signing in

- **Tenant dashboard:** per-person accounts with roles (viewer / agent / admin). Passwords hashed
  with scrypt. Access token 15 minutes, held in memory only; refresh token in an HttpOnly,
  Secure, SameSite=Strict cookie scoped to the auth path, rotated on every use, with reuse
  detection that revokes the whole session family. Login is rate-limited per email (app) and per
  address (Caddy). The tenant comes from the token, never from the URL or request body; request
  bodies reject unknown fields.
- **HMH Labz console:** separate staff accounts with **TOTP required**, single-use codes, a
  token type and audience a tenant token can never satisfy, a 12-hour absolute session, and an
  account check on every request (disabling someone is immediate). The console API answers only
  on the `admin.` host. Only the owner role can record reimbursements.

## Customers

- STOP (and Arabic equivalents) opts out deterministically, before any model call, and a
  campaign can never message an opted-out customer (checked when the audience is built and again
  at send time).
- Marketing consent is recorded with evidence (exact wording shown, time, source). A spreadsheet
  import never opts anyone in.
- The model never produces a number: prices, balances and dates come from tools, and a validator
  rejects replies with numbers the tools did not supply.
- The public consent page runs no script (CSP `default-src 'none'`), is not indexed, sets no
  cookies, stores nothing about the visitor, and is rate-limited.

## Infrastructure

- Only Caddy publishes ports (80/443). Postgres and Redis listen on the internal Docker network
  only. Host firewall: web + SSH (non-standard port, keys only), fail2ban, unattended security
  upgrades (`01_architecture.md` §10.1).
- TLS everywhere via Caddy; HSTS, `nosniff`, frame-deny and a strict CSP on the dashboard.
- Backups: nightly, encrypted with `age` to a public key; the private key is kept off the server,
  so neither the server nor the bucket can read a backup. Restores are tested
  (`test_ops.py`, and the monthly drill in RUNBOOK.md).
- Alerts to HMH Labz's WhatsApp for webhook errors, database outages, queue backlog, LLM
  failover, quality-rating changes, message caps, disk, and backups.

## Supply chain

- Python dependencies locked (`uv.lock`); dashboard dependencies locked and `npm audit` (high)
  runs in CI. CI also runs ruff, mypy `--strict`, the migration down/up check and the full test
  suite on every push.

## Not covered yet (known gaps)

- Model providers (Google, OpenRouter) may process outside the UAE. A UAE-resident alternative
  is in `01_architecture.md` §9 if a client requires it.
- No automatic message-retention period: records are kept for the contract's life unless the
  client instructs deletion.
- Deletion of one person's data on request is a manual operation by HMH Labz.
- Deploys are run by hand on the server (`deploy/deploy.sh`), not by CI.
- No web application firewall in front of Caddy.

## Reporting a problem

Report a suspected vulnerability or data incident to HMH Labz through the contact named in your
contract. Do not include customer data in the first message; we will arrange a secure channel.
