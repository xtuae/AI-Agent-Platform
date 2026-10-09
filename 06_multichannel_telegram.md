# Phase 8 — Channel Layer and Telegram
## Spec change for the Heyozo platform (HMH Labz LLP)

**Version** 1.1 (approved 9 October 2026; built — see §14 for what changed in the build) · 9 October 2026 · extends `01_architecture.md` §4–§5,
`03_build_prompt.md` Phase 1 and the console admin API (`api/platform/admin.py`). Independent of
`05_per_tenant_meta_app.md`, but shares its `channel_key` idea (§4.1 says how the two fit).

**Hand to Claude Code as a single phase, after approval.** Read this whole file first, then build.
It assumes branch `aws-deploy` as of commit `93c7bcc` (console admin screens).

---

## 1. Why this change

Heyozo talks to customers on WhatsApp only. Clients ask for more channels. The agreed order:

| Channel | Decision | Why |
|---|---|---|
| **Telegram** | **Build now** | Official, free Bot API; a client creates a bot in two minutes with @BotFather; no app review, no templates, no per-message cost |
| Instagram DM / Messenger | Later, same layer | Official (Meta Graph API, same App we already run), but needs App Review for `instagram_manage_messages` / `pages_messaging` and has its own 24 h window and tags |
| Web chat widget | Later, same layer | We own both ends; mostly a front-end and an auth-less inbound endpoint |
| **Signal** | **No** | No official bot or business API. The only route is `signal-cli` / unofficial libraries registering a phone number as a client: against Signal's terms, numbers get banned, no SLA, and we would hold a customer's private message store on a reverse-engineered client. Not something to sell to a business. |

The point of this phase is not Telegram alone. It is to stop the platform from assuming WhatsApp in
fifty places, so that each later channel is one adapter, one webhook and one console card.

---

## 2. Where WhatsApp is assumed today

Verified against the code (≈56 non-test files under `api/` mention WhatsApp, `wa_id`, `wamid`,
`phone_number_id` or Meta). Grouped by what each assumption means for a second channel.

### 2.1 Identity — a customer *is* a phone number

| Where | Assumption |
|---|---|
| `customers.wa_id` NOT NULL, `UNIQUE (tenant_id, wa_id)` (`api/db/models/customers.py`, migration 0001) | Every customer has a WhatsApp number; it is the identity |
| `api/webhooks/ingest.py::_persist_inbound` | Upserts the customer `ON CONFLICT (tenant_id, wa_id)`; drops senders failing `_WA_ID` (E.164) |
| `api/meta/outbound.py::_prepare` | Sends to `customer.wa_id` |
| `api/meta/pricing.py::price_message(recipient_wa_id=…)`, `market_for()` | The price market is derived from the phone's country code |
| `api/api/v1/contacts.py`, `common.py::normalise_wa_id`, `conversations.py` (`CustomerRef.wa_id`, search by `wa_id`) | The dashboard identifies and searches contacts by number; creating a contact requires one |
| `api/onboarding/importer.py`, `phones.py`, `scripts/import_excel.py` | Imports are keyed by normalised phone |
| `api/modules/orders/routes.py`, `appointments/routes.py`, `appointments/hooks.py` | Orders/appointments show and look up the customer by number |
| `api/workers/jobs/escalation.py` | The staff alert's `{{2}}` variable is the customer's number |
| `api/modules/campaigns/service.py`, `routes.py`, `sender.py` | Recipients are customers with a `wa_id`; sends go to it |

### 2.2 Message ids and dedup

| Where | Assumption |
|---|---|
| `messages.wamid` UNIQUE (global) | Meta's id is the dedup key, in Redis (`dedup:wamid:*`) and in Postgres |
| `api/meta/statuses.py`, `workers/jobs/webhook_events.py` | Delivery/read statuses arrive by `wamid` |
| `campaign_recipients.wamid`, `optin` evidence, `agents/turn.py` (18 uses), `tools/*`, `recovery.py` | Carry the `wamid` for logging, evidence and status joins |

### 2.3 Channels, routing, webhooks

| Where | Assumption |
|---|---|
| `tenant_channels` (`api/db/models/platform.py`) | Columns are Meta's: `phone_number_id` NOT NULL UNIQUE, `waba_id`, `display_phone`, `access_token_encrypted`, `quality_rating`, `messaging_limit_tier` |
| `api/webhooks/router.py::TenantRouter` | Routes only by `phone_number_id` (and WABA for template updates) |
| `api/webhooks/meta.py`, `payloads.py`, `ingest.py`, `buffer.py`, `signature.py` | One Meta envelope; `X-Hub-Signature-256` against the one app secret |
| `webhook_events.phone_number_id/waba_id` | Raw events are Meta changes |

### 2.4 Sending

| Where | Assumption |
|---|---|
| `api/meta/client.py::MetaClient`, `outbound.py::client_for_channel` | The only sender; workers carry a `client_factory(channel) -> MetaClient` (`workers/arq_app.py`) used by `agents/turn.py`, campaigns, alerts, escalation, the dashboard reply (`api/v1/conversations.py`) |
| `outbound.py::_prepare` → `OutsideServiceWindowError` | Free-form only inside the 24 h customer service window |
| `conversations.service_window_expires_at`, `ingest.SERVICE_WINDOW` | Every inbound opens a 24 h window; the dashboard shows "window open / closed" and refuses replies when closed (`window_closed`) |
| `agents/turn.py::_message_text` | Voice notes are `meta-media:<id>`, downloaded through `MetaClient.download_media` |

### 2.5 Cost, metering, billing

| Where | Assumption |
|---|---|
| `api/metering.py::PricingCategory` (marketing/utility/service/authentication), `usage_daily` columns per category, `meta_cost_aed` | Every outbound message has a Meta category and a price; unpriceable → refuse to send |
| `api/meta/pricing.py`, `pricing` table by market, `billing/*`, `api/v1/costs.py`, `tenants.meta_charges_borne_by_us_until`, reimbursements | Message cost means Meta's charge |

### 2.6 Campaigns, templates, quality

| Where | Assumption |
|---|---|
| `modules/campaigns/*`, `message_templates`, `meta/templates.py`, `template_status` webhooks | A campaign sends an approved Meta template; marketing outside the window requires one |
| `campaigns/guards.py`, `sender.py`, `refresh_quality` job | Pauses on the number's quality rating / messaging tier |
| `campaigns/replies.py` | Attributes replies through `wamid` context |

### 2.7 Consent and STOP

| Where | Assumption |
|---|---|
| `api/optin/*`, `optin_links.prefill`, `api/v1/optin.py` | Consent page → "Continue on WhatsApp" → `wa.me` link with a prefilled `Ref ABCD2345` message; the CSP allows only `https://wa.me` as form action |
| `agents/classifier.py::explicit_optout`, `turn.py::_stop`, `tools/record_opt_out.py` | STOP is a text the customer sends; deterministic |
| `customers.opt_in_*` | Marketing consent per customer, not per channel |

### 2.8 Prompts and formatting

| Where | Assumption |
|---|---|
| `support_v1.j2`, `support_core_v1.j2` | "the WhatsApp assistant", "talking to a customer on WhatsApp", "Two or three short lines. WhatsApp, not email. No headings, no bullet lists" |
| `classifier_*_v1.j2`, `summary_v1.j2` | "latest WhatsApp message", "WhatsApp conversation" |
| `outreach_drafter_v1.j2` | WhatsApp campaign copy under "META TEMPLATE RULES" (≤700 chars, `{{1}}` variables, no links unless the domain is verified) |
| `agents/canned.py`, `compose.py` | "Reply STOP" wording |
| `agents/validator.py` | `MAX_REPLY_CHARS = 700`: longer replies are regenerated. No markup conversion or message splitting exists anywhere today |

### 2.9 Alerts

`api/alerts/send.py` sends platform alerts to HMH staff as a WhatsApp utility template from a
designated tenant's channel; `escalation.py` alerts the client's escalation phone the same way.
These are staff notifications, not customer conversations. **They stay on WhatsApp in this phase.**

### 2.10 Dashboard and console

API surfaces that name WhatsApp: `api/v1/today.py` (a "whatsapp" health check: no active
number, token expiry; "live" = window open), `api/platform/console.py` (`ChannelOut` with
display_phone / quality / tier; "no active WhatsApp number"), `api/billing/statement.py` (pulls the
Meta statement from "a WhatsApp account with a token"), `api/v1/optin.py` (`whatsapp_ready`, default
heading "…on WhatsApp"), `api/v1/settings.py` (escalation phone via `normalise_wa_id`).

Client dashboard: contacts list/detail (number as identity), conversation list and thread
(window badge, reply box disabled when the window is closed), campaigns (templates), opt-in links
("Continue on WhatsApp"), costs (Meta categories), settings (escalation phone). Console:
Client → Setup lists **WhatsApp numbers** with add / edit / set-token
(`dashboard/src/console/admin.tsx`).

---

## 3. Design

### 3.1 Principles

1. **A channel adapter owns everything channel-specific**; the agent, the turn runner, the
   dashboard and metering talk to an interface. No `if kind == "telegram"` outside `api/channels/`
   and the per-channel webhook module — capabilities are data on the adapter.
2. **WhatsApp behaviour is byte-identical.** Every existing test passes unmodified except where a
   test imports a moved symbol (re-exports keep even those working). Aquamena needs no
   reconfiguration.
3. **Verify before parse**, per channel (SECURITY.md). Each channel authenticates its webhook its
   own way, on the raw request, before a byte is parsed.
4. **The tenant comes from the URL or the verified payload, never a guess**, and the two must agree
   (05 §2.3 step 5 discipline).

### 3.2 Channel kinds and capabilities

```python
# api/channels/base.py
ChannelKind = Literal["whatsapp", "telegram"]          # later: "instagram", "messenger", "web"

@dataclass(frozen=True)
class Capabilities:
    service_window: timedelta | None   # whatsapp 24h; telegram None (no window)
    priced: bool                       # whatsapp True (Meta charges); telegram False
    templates: bool                    # whatsapp True; telegram False
    delivery_receipts: bool            # whatsapp True (sent/delivered/read); telegram False
    max_text_chars: int                # 4096 both
    formatting: Literal["whatsapp", "telegram_html", "plain"]
    identity_is_phone: bool            # whatsapp True; telegram False
```

### 3.3 Inbound: one normalised shape

Each channel's webhook verifies, parses its own envelope and produces:

```python
@dataclass(frozen=True)
class InboundMessage:
    channel_kind: ChannelKind
    external_user_id: str          # wa_id | Telegram user id
    external_message_id: str       # wamid | "tg:<bot_id>:<chat_id>:<message_id>"
    sent_at: datetime
    msg_type: str                  # text | audio | image | … (existing vocabulary)
    text: str | None
    media_ref: str | None          # "meta-media:<id>" | "tg-file:<file_id>"
    profile_name: str | None
    username: str | None           # Telegram @username, None on WhatsApp
    start_payload: str | None      # Telegram /start deep-link payload
```

`_persist_inbound` becomes channel-agnostic (`api/channels/persist.py`): identity upsert →
conversation upsert (window set only when `caps.service_window`) → message insert keyed on
`external_message_id` → usage. The Meta ingestor maps its payload into `InboundMessage` and calls
it; its routing, dedup, buffer and status paths are unchanged.

### 3.4 Outbound: one sender interface

```python
class ChannelSender(Protocol):
    kind: ChannelKind
    caps: Capabilities
    async def send_text(self, to: str, text: str) -> SendResult        # SendResult.external_id
    async def download_media(self, ref: str) -> MediaDownload
    async def typing(self, to: str) -> None                            # no-op on WhatsApp
```

* `WhatsAppSender` wraps `MetaClient` (unchanged). Template, interactive, quality and pricing calls
  stay on `MetaClient`; campaigns and alerts keep using it directly.
* `TelegramSender` wraps the new `TelegramClient`.
* `sender_for(channel, http, settings) -> ChannelSender` replaces `client_for_channel` at the
  generic call sites. Workers' `client_factory` becomes `sender_factory`; `client_factory` stays
  for Meta-only code (campaign templates, alerts, quality).
* `api/meta/outbound.py::send_text_reply` moves to `api/channels/outbound.py` and becomes
  capability-driven: window check only if `caps.service_window`; price via `price_message` only if
  `caps.priced`, otherwise cost 0 and category `service`; the rest (record message + meter in one
  transaction, `outbound_unrecorded` CRITICAL) is unchanged. `send_template` stays in
  `api/meta/outbound.py`. Old import paths re-export.
* Formatting: the agent keeps writing WhatsApp-style markup (one prompt, one validator). The
  Telegram sender converts it (§5.5). Splitting at `max_text_chars` happens in the sender.

### 3.5 Customer identity

A customer is a person; an identity is how a channel knows them.

```
customer_identities
  tenant_id, id, customer_id  (FK (tenant_id, customer_id) → customers)
  kind            text   CHECK in ('whatsapp','telegram')
  external_id     text   -- wa_id (E.164 digits) | Telegram user id (digits)
  username        text   -- Telegram @username, display only
  display_name    text
  blocked_at      timestamptz   -- Telegram: user blocked the bot; sends refused until unblocked
  first_seen_at, last_seen_at
  UNIQUE (tenant_id, kind, external_id)
  UNIQUE (tenant_id, customer_id, kind)   -- one identity per channel kind per customer
  RLS forced, tenant policy like every tenant table
```

* `customers.wa_id` **stays** as the denormalised WhatsApp number (now nullable). Everything in
  §2.1 that reads it keeps working for WhatsApp customers; a Telegram-only customer has
  `wa_id IS NULL`. `UNIQUE (tenant_id, wa_id)` still holds (NULLs are distinct).
* Inbound upsert: under `pg_advisory_xact_lock(hashtext(tenant||kind||external_id))`, find the
  identity → else create customer (with `wa_id` set when kind is whatsapp) + identity. The lock
  stops two simultaneous first messages from creating two customers (WhatsApp already had the
  unique `wa_id` for this; Telegram needs the lock).
* **No automatic merging across channels.** A Telegram user and a WhatsApp number are two customers
  until a person links them (follow-up: "Merge contacts" in the dashboard). Telegram does not give
  us a phone number unless the user shares it, and guessing would cross-wire people.
* Outbound picks the identity of the conversation's channel kind. Conversations already carry
  `channel_id` and are unique per (customer, channel) — no change.

### 3.6 Message ids and dedup

`messages.wamid` keeps its name and its global UNIQUE, and is documented as **the channel's message
id**. Telegram ids are namespaced so they can never collide with Meta's (`wamid.…`):
`tg:<bot_id>:<chat_id>:<message_id>`. Renaming the column touches ~22 files for no behavioural
gain; it is listed as a cleanup, not done here. Redis dedup for Telegram claims
`dedup:tg:<channel_id>:<update_id>` (Telegram's own redelivery key) before the DB unique backstop.

---

## 4. Data model — migration `0009_channels_telegram.py`

Additive. No existing row changes meaning. (If `05_per_tenant_meta_app.md` is built first, this
becomes 0010 and reuses its `channel_key` column instead of adding it.)

### 4.1 `tenant_channels`

```sql
ALTER TABLE tenant_channels
  ADD COLUMN kind                   text NOT NULL DEFAULT 'whatsapp',
  ADD COLUMN channel_key            text,         -- as 05 §2.1; backfilled per row in Python
  ADD COLUMN telegram_bot_id        text,         -- numeric id from getMe
  ADD COLUMN telegram_username      text,         -- @handle, display
  ADD COLUMN webhook_secret_hash    bytea,        -- sha256 of the secret_token we gave Telegram
  ADD COLUMN webhook_set_at         timestamptz,
  ADD COLUMN webhook_error          text,         -- last setWebhook/getWebhookInfo error, no secrets
  ALTER COLUMN phone_number_id DROP NOT NULL;

UPDATE … channel_key = secrets.token_urlsafe(24) per row;   -- CSPRNG, in Python
ALTER TABLE tenant_channels
  ALTER COLUMN channel_key SET NOT NULL,
  ADD CONSTRAINT tenant_channels_channel_key_uq UNIQUE (channel_key),
  ADD CONSTRAINT tenant_channels_telegram_bot_uq UNIQUE (telegram_bot_id),
  ADD CONSTRAINT tenant_channels_kind_ck CHECK (kind IN ('whatsapp','telegram')),
  ADD CONSTRAINT tenant_channels_kind_fields_ck CHECK (
       (kind = 'whatsapp' AND phone_number_id IS NOT NULL AND telegram_bot_id IS NULL)
    OR (kind = 'telegram' AND phone_number_id IS NULL AND telegram_bot_id IS NOT NULL));
```

* The bot token goes in the existing **`access_token_encrypted`** (Fernet, same key): it is the
  channel's sending credential, already redacted in logs and `__repr__`.
* The webhook secret is random (`secrets.token_urlsafe(48)`, inside Telegram's 1–256
  `[A-Za-z0-9_-]` rule), sent to Telegram once in `setWebhook`, and **only its sha256 is stored** —
  we never need the plaintext again, so there is nothing to decrypt or leak. Re-registering mints a
  new one.
* `phone_number_id` UNIQUE keeps working (NULLs distinct). `TenantRouter` is untouched: its SQL
  looks up by `phone_number_id`, which a Telegram row never matches.
* Add `webhook_secret_hash` and `channel_key` to the logging redaction list.

### 4.2 `customer_identities`

Created as in §3.5, with RLS enabled and forced and the standard tenant policy. Backfilled **per
tenant** (RLS is forced; same loop as 0008): one `('whatsapp', wa_id)` identity per existing
customer. Then `ALTER TABLE customers ALTER COLUMN wa_id DROP NOT NULL`.
A check `customers_has_identity` cannot be a SQL constraint (cross-table); a test asserts it instead.

### 4.3 Other tables

* `webhook_events`: `ADD COLUMN channel_id uuid NULL` (composite FK to `tenant_channels`); kind
  gains `'telegram'`. Telegram updates are stored like Meta changes (raw, under the tenant).
* `messages`: none (see §3.6).
* `conversations`: none. `service_window_expires_at` stays NULL for Telegram conversations; the
  API reports `window_open: true, window_expires_at: null` for channels without a window.

### 4.4 Downgrade

Refuses (raises) if any `kind <> 'whatsapp'` channel or any customer with `wa_id IS NULL` exists —
dropping them would orphan conversations. Otherwise drops the identity table and columns.
Migration round-trip test as in 05 §5.

---

## 5. Telegram specifics

### 5.1 New modules

```
api/channels/
  base.py        ChannelKind, Capabilities, InboundMessage, SendResult, ChannelSender
  registry.py    sender_for(), caps_for(kind)
  persist.py     persist_inbound() (from ingest._persist_inbound), identity upsert
  outbound.py    send_text_reply() (capability-driven, from meta/outbound.py)
  whatsapp.py    WhatsAppSender(MetaClient)
  telegram/
    client.py    TelegramClient: get_me, set_webhook, delete_webhook, get_webhook_info,
                 send_message, send_chat_action, get_file, download_file
    payloads.py  Update, Message, User, Chat, Voice, Audio, PhotoSize, Document, ChatMemberUpdated
    webhook.py   POST /webhook/telegram/{channel_key}
    ingest.py    TelegramIngestor: route → dedup → normalise → persist → enqueue
    router.py    resolve_channel_key() — 05's channel_lookup, Telegram kind only for now
    format.py    WhatsApp markup → Telegram HTML; split to 4096
```

`TelegramClient` mirrors `MetaClient`: shared `httpx.AsyncClient`, base URL from settings
(`telegram_api_base_url = "https://api.telegram.org"`, overridable in tests), bounded retries,
honours `429 parameters.retry_after`, `__repr__` without the token. **The token is in the URL path**
(`/bot<token>/method`) — so httpx request logging must never log URLs for this host, errors are
re-raised with the method name only, and a test asserts no token appears in any log line.

### 5.2 Webhook — `POST /webhook/telegram/{channel_key}`

```
1. channel_key shape ^[A-Za-z0-9_-]{16,64}$                          bad → 404, no DB query
2. resolve key → channel (Redis 300 s / 60 s negative, Postgres fallback)
     must be kind='telegram', is_active, tenant status trial|active  else → 404 (same body for all)
3. X-Telegram-Bot-Api-Secret-Token: sha256(header) compare_digest webhook_secret_hash
     missing / wrong → 403, logged with channel id only
4. body size ≤ 1 MB (Telegram updates are small) — then, and only then, parse Update
     unparseable → 200 and drop (a retry will not parse either)
5. accept only private chats; ignore groups, channels, edited messages, service updates → 200
6. Redis claim dedup:tg:<channel_id>:<update_id>; DB unique on the namespaced message id
7. persist (tenant session from step 2) + enqueue the existing handle_inbound_message job → 200
   database down → 503: Telegram keeps the update (up to 24 h) and redelivers. No Redis buffer
   for Telegram — Telegram is the buffer.
```

The channel key fixes the tenant before parsing, and the secret proves the request came from the
Telegram registration we made for *that* channel. Cross-check (05 §2.3 step 5): the update carries
no bot id, so the check is that the key's channel has a hash and the header matches it — a header
valid for bot A presented on bot B's path fails step 3.

Optional defence in depth (setting, default off): also require the source IP in Telegram's
published ranges (149.154.160.0/20, 91.108.4.0/22). Off by default because Cloudflare/Caddy may
mask the source; documented in RUNBOOK.

`my_chat_member` updates: `kicked` → set `customer_identities.blocked_at`; `member` → clear it. A
blocked identity is never sent to; a `403 Forbidden: bot was blocked by the user` on send sets it
too. Blocking is **not** a marketing opt-out (the customer did not say STOP); it is unreachability.

### 5.3 setWebhook is done by the server

When a bot is added (or its token rotated, or "Re-register" is pressed, or the channel is
re-activated), the server calls

```
setWebhook(url="https://api.<BASE_DOMAIN>/webhook/telegram/<channel_key>",
           secret_token=<new random>, allowed_updates=["message","my_chat_member"],
           max_connections=10, drop_pending_updates=false)
```

then `getWebhookInfo` to confirm the URL, and stores `webhook_set_at` / `webhook_error`.
Deactivating a Telegram channel calls `deleteWebhook` (otherwise Telegram keeps retrying into a 404).
New setting `public_api_base_url` (required for Telegram; startup logs an error if a Telegram
channel exists without it). The health watchdog gains a daily `getWebhookInfo` check per active bot
that alerts on `last_error_message` or `pending_update_count > 100`.

### 5.4 Voice notes and media

Telegram voice notes are OGG/Opus — the same format as WhatsApp's, which `api/llm/transcribe.py`
already handles. `media_ref = "tg-file:<file_id>"`; `TelegramSender.download_media` calls `getFile`
then downloads `file/bot<token>/<file_path>`, capped at `meta_media_max_bytes` and Telegram's 20 MB
`getFile` limit. `turn.py::_message_text` switches on the ref prefix through the sender instead of
hard-coding `meta-media:`. Photos/documents are stored as type + caption like WhatsApp's today.

### 5.5 Message length and formatting

* The validator's `MAX_REPLY_CHARS = 700` stays the agent's limit on both channels (it is a style
  rule, not a transport one). Telegram's transport limit is 4096 characters after entity parsing,
  the same as WhatsApp's; dashboard replies and campaign bodies can exceed 700, so the sender splits
  longer texts at paragraph, then sentence, boundaries (never inside a tag), sends parts
  in order, records one `messages` row per part.
* The agent writes WhatsApp markup. `format.py` converts to Telegram **HTML** parse mode (not
  MarkdownV2, whose 18 reserved characters make LLM text fail constantly): escape `& < >`, then
  `*x*`→`<b>`, `_x_`→`<i>`, `~x~`→`<s>`, `` ```x``` ``→`<pre>`, `` `x` ``→`<code>`. Unbalanced markers are
  left as literal text. If Telegram still answers `400 can't parse entities`, resend once as plain
  text — never drop a reply over formatting.
* `link_preview_options.is_disabled = true` (WhatsApp sends without previews today).
* `sendChatAction typing` before an LLM turn (cheap, makes the bot feel live). Best-effort.
* Rate limits: ~1 msg/s per chat, ~30 msg/s per bot. Replies are far below; the campaign sender
  throttles to 20/s per bot and honours `retry_after`.

### 5.6 Prompts

The support, classifier and summary templates and the canned texts get a `{channel_name}` variable
("WhatsApp" / "Telegram") wherever they name the channel ("WhatsApp, not email" becomes
"{channel_name}, not email"). Formatting rules stay as written (the sender converts). The outreach
drafter gets a Telegram variant without the Meta template rules (no `{{n}}` limits, links allowed). Prompt version bumps so
logs show which turns ran on the new templates.

---

## 6. What campaigns, consent and STOP mean on Telegram

| Concept | WhatsApp (today) | Telegram |
|---|---|---|
| Who can be messaged | Anyone with a number; outside 24 h only by approved template | **Only users who started this bot** and have not blocked it — Telegram enforces this (403) |
| Service window | 24 h after the customer's last message | None |
| Templates | Required for campaigns and out-of-window | **None.** A campaign is free text (same variables), no approval |
| Cost per message | Meta's, by market and category; metered | **Zero.** Metered as messages with cost 0 |
| Quiet hours, budget guard | 22:00–08:00 Gulf time; Meta spend cap | Quiet hours **apply** (same rule, it is about people); budget guard is a no-op (cost 0) |
| Quality guard | Number quality rating / tier | **Block-rate guard**: pause a campaign when >3 % of its sends return "blocked" within the first 200 |
| Marketing consent | `opt_in_status = opted_in` with evidence | **Same rule** — starting a bot is permission to *reply*, not to market. Campaigns go only to `opted_in` customers with an unblocked Telegram identity |
| Consent capture | `/q/<code>` page → `wa.me` prefilled `Ref XXXX` | Same page → **"Continue on Telegram"** → `t.me/<bot>?start=<REF>`; the bot receives `/start <REF>` and claims the ref exactly as `find_ref` does today (the normaliser exposes `start_payload`) |
| STOP | Text matched by `explicit_optout` | Same, plus `/stop` command. Confirmation reply sent once; no further marketing |
| Blocked the bot | — | Unreachable (`blocked_at`); not an opt-out; shown in the dashboard |

**Campaigns in this phase**: the campaign model gains a channel. A Telegram campaign has a text
body instead of a template, skips pricing and the quality guard, uses the block-rate guard and the
20/s throttle. If the review finds campaigns too large for one phase, the fallback is to ship
Telegram campaigns disabled (API returns `409 channel_not_supported`) and add them next — **this is
decision D3 in §12.**

---

## 7. Console — Client → Setup → Telegram bot

Next to **WhatsApp numbers**, a **Telegram bot** card (a client has at most one bot in this phase;
the API allows more).

```
POST  /api/v1/platform/admin/tenants/{tenant_id}/telegram      {token}            ops
PUT   /api/v1/platform/admin/channels/{channel_id}/telegram-token {token}         ops
POST  /api/v1/platform/admin/channels/{channel_id}/telegram-webhook               ops  (re-register)
PATCH /api/v1/platform/admin/channels/{channel_id}             is_active          ops  (existing; now also delete/set webhook for Telegram)
```

Add-bot flow (mirrors `set_token`):

1. Validate the token's shape (`^\d{5,16}:[A-Za-z0-9_-]{30,64}$`) — reject before any network call.
2. `getMe` with it. Not a bot / 401 → `422 telegram_rejected`. **A token Telegram rejects is never
   stored.**
3. `getMe.id` already on another channel → `409 bot_taken {same_tenant}` (never reroute a live bot).
4. Mint `channel_key` and webhook secret; `setWebhook`; `getWebhookInfo` confirms.
5. Insert the channel: `kind='telegram'`, `telegram_bot_id`, `telegram_username`,
   `access_token_encrypted = encrypt(token)`, `webhook_secret_hash`, `webhook_set_at`.
   If the insert fails after step 4, `deleteWebhook` best-effort.
6. Audit `add_telegram_bot` with `{bot_username, bot_id}` — never the token. Log likewise.

Token rotation (`/revoke` in BotFather): same checks, and `getMe.id` must equal the channel's
`telegram_bot_id` (`409 different_bot`), then re-`setWebhook` with a fresh secret.

`ChannelAdmin` gains `kind`, `telegram_username`, `webhook_ok`, `webhook_error`; `phone_number_id`
becomes optional. **No response ever contains a token or webhook secret.** The UI: paste field
(type password, autocomplete off), "Check and connect", then the card shows `@username`, webhook
status, Re-register, Replace token, Deactivate. The token is never shown again.

---

## 8. Client dashboard

* Contacts: identity column shows the number for WhatsApp, `@username` / name with a Telegram badge
  for Telegram; search matches `username` and `external_id` as well as `wa_id`. Creating a contact
  by hand still requires a WhatsApp number (Telegram contacts arrive only by starting the bot).
* Conversations: channel badge per conversation; the window badge and the "window closed" reply
  lock only for channels with a window; filter by channel.
* Opt-in links: if the client has a bot, the consent page shows "Continue on Telegram" next to (or
  instead of) WhatsApp; the CSP `form-action` adds `https://t.me`.
* Costs: Telegram messages counted, cost 0; a per-channel split in the message counts.
* Campaigns: channel picker; Telegram shows a text body editor instead of the template picker.
* Settings: nothing new.

API contract: `CustomerRef` / contact payloads gain `channels: [{kind, handle}]`; `wa_id` becomes
nullable. The dashboard types and every screen that shows `wa_id` handle `null`.

---

## 9. Code changes, file by file (summary)

| File | Change |
|---|---|
| `api/channels/**` | New (§5.1) |
| `api/db/models/platform.py` | `TenantChannel` new fields; `phone_number_id` optional |
| `api/db/models/customers.py` | `wa_id` optional; new `CustomerIdentity` |
| `api/db/models/webhooks.py` | `channel_id` |
| `api/db/migrations/versions/0009_channels_telegram.py` | §4 |
| `api/webhooks/ingest.py` | Maps Meta messages to `InboundMessage`, calls `channels.persist` |
| `api/meta/outbound.py` | `send_text_reply` moves to `channels/outbound.py` (re-exported); `send_template` stays |
| `api/agents/turn.py` | `sender_factory`; media by ref prefix; typing action; `/start` payload into the consent path; `/stop` |
| `api/agents/prompts/*`, `canned.py` | `{channel_name}` |
| `api/workers/arq_app.py` | `sender_factory`; Telegram webhook-health job |
| `api/api/v1/conversations.py`, `contacts.py`, `common.py` | Sender factory for replies; identities in payloads; window per capability; `whatsapp_rejected` → `channel_rejected` (old code kept as an alias) |
| `api/api/v1/today.py`, `api/platform/console.py` | Health check per channel kind ("no active channel", bot webhook error); `ChannelOut.kind` |
| `api/billing/statement.py`, `api/workers/jobs/escalation.py`, `api/alerts/send.py` | Select **WhatsApp** channels only (`kind='whatsapp'`) — today they take any active channel; escalation's customer variable falls back to `@username (Telegram)` |
| `api/onboarding/*`, `api/scripts/import_excel.py` | Unchanged (imports stay WhatsApp numbers); identity created for each imported customer |
| `api/modules/campaigns/*` | Channel on campaign; Telegram text path; block-rate guard (D3) |
| `api/optin/*` | Telegram button and deep link; CSP |
| `api/platform/admin.py` | §7 routes |
| `api/main.py`, `api/config.py` | Router; `telegram_api_base_url`, `public_api_base_url`, `telegram_ip_check` |
| `api/core/logging.py` | Redact `channel_key`, `webhook_secret_hash`; drop URLs for the Telegram host |
| `dashboard/src/console/admin.tsx` | Telegram bot card |
| `dashboard/src/**` | §8 |
| `Caddyfile` | `/webhook/telegram/*`: same treatment as `/webhook/meta` (no tight rate limit, raw body) |
| `SECURITY.md`, `RUNBOOK.md` | Telegram section; §11 |

---

## 10. Tests

New `api/tests/test_telegram.py` (plus extensions), in the existing style, with a mocked Telegram
API (httpx `MockTransport`, like `test_meta_client.py`).

| # | Case | Expect |
|---|---|---|
| 1 | Valid update, right secret | 200, customer + identity + conversation + message under the right tenant, job enqueued |
| 2 | Wrong / missing secret header | 403, nothing parsed (malformed JSON body still 403, not 200), no DB write |
| 3 | Bot A's secret on bot B's path | 403 |
| 4 | Unknown / malformed / path-traversal key | 404, no Postgres query for malformed |
| 5 | Inactive channel; suspended tenant; WhatsApp channel's key on the Telegram path | 404 |
| 6 | Same `update_id` twice; Redis down twice | one message; DB unique backstops |
| 7 | Two simultaneous first messages from a new user | one customer, one identity |
| 8 | Group / channel / edited message | 200, ignored |
| 9 | Voice note | downloaded via getFile, transcribed with the existing transcriber, transcript stored |
| 10 | Reply > 4096 chars; markup conversion; 400 parse error → plain resend | split in order; HTML correct; reply delivered |
| 11 | Telegram outbound | no window check, no pricing call, cost 0, usage metered |
| 12 | Send returns 403 blocked; `my_chat_member` kicked / member | `blocked_at` set / cleared; no further sends |
| 13 | `/start <REF>` from a consent page | opted in with evidence, exactly like the WhatsApp ref |
| 14 | STOP and `/stop` | opted out, one confirmation |
| 15 | Telegram campaign (D3) | text only to opted-in, unblocked identities; throttle; block-rate guard pauses |
| 16 | DB down | 503, nothing buffered, redelivery later processes it once |
| 17 | Token / secret never logged | capture all logs across 1–16 and the console flows; assert neither appears |
| 18 | WhatsApp unchanged | the whole existing suite green; `test_webhook_routing.py` unmodified |

Console (`test_platform_admin.py`): add bot (getMe ok → setWebhook called with key URL and a
secret; stored encrypted; response has no token), rejected token not stored, bot taken (409),
rotate to a different bot (409), re-register, deactivate calls deleteWebhook, support role refused
writes. **`test_dashboard_isolation.py`**: every new route gets a case (the route-table test
enforces it) — the three new console routes in the `_console_refused` table (a tenant token gets
401), and the changed tenant routes (contacts, conversations, campaigns) re-checked with tenant B
holding a Telegram customer, identity and campaign marked with `B_MARK`. `test_tenant_isolation.py`:
RLS on `customer_identities` (cross-tenant read returns nothing, write refused).
Regression: a tenant with a WhatsApp and a Telegram channel — billing statement, escalation and
alerts still pick the WhatsApp channel. `test_migrations.py`: round-trip; every existing customer gets exactly one WhatsApp identity;
downgrade refuses with a Telegram channel present.

---

## 11. Acceptance

1. All 662 existing tests green, plus the above; `ruff` and `mypy --strict` clean; dashboard
   typecheck, tests and build pass. Run in throwaway containers on the EC2 test runner
   (`~/hmhtest-run.sh`), never against production.
2. Aquamena's WhatsApp channel and every existing customer work with no configuration change.
3. A Telegram bot added in the console answers a text and a voice note end to end on a staging
   tenant (manual check after deploy; the user decides when).
4. No token or webhook secret appears in any response, log line or audit row.

### What a client does to connect a bot (goes in RUNBOOK and the client guide)

1. In Telegram, open **@BotFather** → `/newbot` → choose a name and a username ending in `bot`.
2. Copy the token BotFather shows and send it to HMH Labz through the agreed secure channel (not
   email in clear). Optionally set the bot's photo, description and `/setjoingroups → Disable`.
3. HMH Labz pastes it into Console → Client → Setup → Telegram bot → Check and connect. Done: the
   server registers the webhook.
4. The client shares `t.me/<their_bot>` (or the consent-page QR) with customers. Customers must
   press **Start** once; after that the bot can answer and, with consent, send campaigns.
5. If the token leaks: BotFather `/revoke`, then Replace token in the console.

---

## 12. Decisions for approval

| # | Decision | Recommendation |
|---|---|---|
| D1 | Keep `customers.wa_id` (nullable) + new `customer_identities`, rather than moving all reads to identities now | **Yes** — small blast radius; full move later |
| D2 | Keep the column name `messages.wamid`, namespaced Telegram ids | **Yes** — rename as a later cleanup |
| D3 | Telegram campaigns in this phase | **Yes** (text-only, block-rate guard); fallback: ship disabled |
| D4 | Bot token pasted in the console (not CLI-only as 05 §9 said for app secrets) | **Yes** — the console already takes Meta tokens; same pattern, checked against Telegram first |
| D5 | No Redis buffer for Telegram; 503 and let Telegram redeliver | **Yes** |
| D6 | Staff alerts and escalation alerts stay on WhatsApp | **Yes**; a Telegram-only client sees escalations in the dashboard only (follow-up: alert into a staff Telegram chat) |
| D7 | No automatic cross-channel customer merge | **Yes**; manual merge is a follow-up |

---

## 13. Out of scope

* **Signal** — no official bot or business API; `signal-cli` and similar register as a personal
  client against Signal's terms and get numbers banned. Revisit only if Signal ships a business API.
* **Instagram DM / Facebook Messenger** — follow-up on this layer: `kind` values, a Meta webhook
  object (`instagram` / `page`) routed by page/IG account id, a `MetaMessengerSender`, the 24 h
  window and human-agent tag as capabilities, App Review for the permissions.
* **Web chat widget** — follow-up: `kind='web'`, a signed embed key per channel, a public inbound
  endpoint with rate limits and a visitor cookie as the identity, SSE/WebSocket for replies.
* Telegram groups, channels, inline mode, payments, bot commands menu beyond `/start` and `/stop`,
  sending media from the agent.
* Customer merge across channels; renaming `wamid`; moving `customers.wa_id` reads to identities.
* Deploying. The user decides when.

---

## 14. As built — where the build differs from this plan

Decisions D1–D7 were approved as recommended. Differences found while building:

* **WhatsApp identities are kept by a trigger.** `customers.wa_id` → `customer_identities`
  ('whatsapp') is maintained by `app_sync_whatsapp_identity()` on insert/update of `wa_id`, so
  every existing path that creates or edits a customer (ingest, contacts, imports, tests) stays
  correct without changes. Telegram identities are written by `api/channels/inbound.py`.
* **The Meta ingest keeps its own persist path** (byte-identical behaviour, as §3.1 rule 2
  requires); `persist_inbound` in `api/channels/inbound.py` is used by Telegram and is the path
  for future channels.
* **A long Telegram reply is one `messages` row** (the whole text), recorded under the id of its
  first part; Telegram has no delivery receipts to reconcile per part.
* **Prompts:** the released v1 templates stay verbatim for WhatsApp. On another channel the same
  release is compiled with "WhatsApp" replaced by the channel's name, and the turn records
  `prompt_version` with an `@telegram` suffix. The agent's 700-character reply limit is unchanged.
* **The request log redacts the channel key** (`/webhook/telegram/{channel_key}`); found by the
  "secrets never logged" test.
* **Migration constraint names use `op.f()`**: the repo's naming convention otherwise prefixes
  explicit names twice (existing constraints such as `ck_optin_visits_ck_optin_visits_claim`
  already carry the doubled name; left as they are).
* **Downgrade** refuses while Telegram channels or number-less customers exist, as planned; the
  migration round-trip test first removes the Telegram rows earlier tests created.
* **Campaign block-rate guard** is evaluated after every send once 30 sends exist (not only after a
  blocked one), so it does not depend on recipient order.
* **Escalation alerts** for a Telegram conversation go out from the tenant's WhatsApp number (if
  any), naming the customer as `@username (Telegram)`; with no number, the dashboard only.
* `api/core/middleware.py` webhook metrics and the Meta buffer/watchdog stay Meta-only.
