# Module spec — Appointments (draft for approval)

**Status** Draft v0.1 · 29 Sep 2026 · for HMH Labz approval before any code is written
**Serves** real-estate viewings, law-firm consultations, and any business that books time slots.
**Presets** `real_estate` = appointments (+ listings, leads later) · `law_firm` = appointments (+ intake, matters later)

---

## 1. What it does

A customer can **book, move, cancel or check** an appointment on WhatsApp, and the team can do
the same from the dashboard. No double bookings, and no time is ever offered that did not come
from the availability tool.

## 2. Data (tenant tables, RLS like every other)

| Table | Fields | Notes |
|---|---|---|
| `appointment_types` | name_en, name_ar, duration_min, location_kind (`office` / `onsite` / `video` / `phone`), needs_address, is_active | e.g. "Property viewing (30 min, on site)", "Initial consultation (45 min, office)" |
| `appointment_resources` | name, tenant_user_id (optional), is_active | who or what is booked: an agent, a lawyer, a meeting room |
| `availability_rules` | resource_id, weekday, start_time, end_time | weekly hours per resource |
| `availability_exceptions` | resource_id (or all), date, start/end (or whole day), reason | holidays, leave |
| `appointments` | customer_id, type_id, resource_id, starts_at, ends_at, status (`requested` / `confirmed` / `cancelled` / `completed` / `no_show`), location_note, notes, source (`agent` / `dashboard`), conversation_id, created_by | **Postgres exclusion constraint** on (resource_id, time range) for live statuses, so two bookings cannot overlap even under a race |

## 3. Agent tools

| Tool | Purpose | Guards |
|---|---|---|
| `get_availability(type, from_date, to_date)` | free slots | only within `min_notice_hours` … `horizon_days`; at most ~8 slots returned |
| `book_appointment(type, starts_at, notes?)` | book a slot the customer confirmed | slot must be one the tool offered and still free; exclusion constraint is the final word; idempotent per conversation |
| `get_my_appointments()` | this customer's upcoming appointments | own customer only |
| `reschedule_appointment(ref, new_starts_at)` | move | inside `cancel_cutoff_hours` → escalate instead |
| `cancel_appointment(ref)` | cancel | inside cutoff → escalate instead |

**Prompt pieces** (same mechanism as orders):
- WHAT YOU CAN DO: "Book, move or cancel appointments".
- Rule 1 facts: "an appointment time".
- Rule: "NEVER confirm a booking until you have said back the type, date, time and place, and they have agreed."
- BOOKING FLOW: work out the type → offer 2–3 slots from `get_availability` → confirm → `book_appointment` → give the reference.
- Classifier intent: `booking` ("wants to book, move, cancel or check an appointment").

## 4. Dashboard

- **Appointments screen**: a day and week agenda (a list on phones), filters by resource and type; create, move, cancel, and mark completed or no-show.
- **Today tile**: today's appointments, and the next one.
- **Contact panel**: upcoming and past appointments.
- **Settings section**: types, resources, weekly hours, exceptions.

## 5. Per-client config (`tenant_modules.config`)

`slot_minutes` (15/30/60), `min_notice_hours` (default 2), `horizon_days` (default 30),
`cancel_cutoff_hours` (default 24), `require_team_confirmation` (bookings land as `requested`
and a person confirms; default false), `buffer_minutes` between appointments (default 0).

## 6. Out of scope for v1 (say if any are needed now)

- Google/Outlook calendar sync.
- **Reminders** before an appointment. These need approved utility templates, so they come with Phase 4 (templates and campaigns).
- Deposits or payment.
- Group bookings.
- Customer-chosen resource ("I want lawyer X"). The agent picks any free resource unless the type is tied to one.

## 7. Questions for HMH Labz

1. Do real-estate viewings happen **at the property**? If so, a viewing is tied to a listing, which depends on the future `listings` module. Should v1 capture the property as free text in notes?
2. Should law-firm consultations be **confirmed by a person** before they're final (`require_team_confirmation`)?
3. Do you have a real estate or law firm client lined up whose actual workflow we should match?
