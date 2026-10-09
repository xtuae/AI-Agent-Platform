import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ContactPicker, useContactNoun } from "@/components/ContactPicker";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { contactHandle, dateTime, label, localToday, STATUS_LABEL } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { CustomerDetail, CustomerRow, Page } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useHasModule } from "@/modules";
import { addDays, dayLabel, hhmm, LOCATION_LABEL, plusMinutes, StatusBadge, weekStart } from "./shared";
import type { Appointment, AppointmentDetail, AppointmentStatus, AppointmentType, Resource, Slot } from "./types";

const FILTERS: { key: string; label: string; status?: AppointmentStatus[] }[] = [
  {
    key: "active",
    label: "Booked",
    status: ["requested", "confirmed", "completed", "no_show"],
  },
  { key: "requested", label: "To confirm", status: ["requested"] },
  { key: "all", label: "All, with cancelled" },
];

const ACTION_LABEL: Record<AppointmentStatus, string> = {
  confirmed: "Confirm",
  completed: "Mark completed",
  no_show: "No-show",
  cancelled: "Cancel appointment",
  requested: "Back to requested",
};

export default function AppointmentsPage() {
  const [params, setParams] = useSearchParams();
  const view = params.get("v") === "day" ? "day" : "week";
  const filter = params.get("f") ?? "active";
  const resource = params.get("resource") ?? "";
  const type = params.get("type") ?? "";
  const today = localToday();
  const anchor = params.get("d") ?? today;
  const from = view === "day" ? anchor : weekStart(anchor);
  const to = view === "day" ? anchor : addDays(from, 6);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const canEdit = useCan("agent");

  const query = {
    from,
    to,
    status: FILTERS.find((f) => f.key === filter)?.status,
    resource_id: resource || undefined,
    type_id: type || undefined,
  };
  const list = useQuery({
    queryKey: ["appointments", query],
    queryFn: () => api<Page<Appointment>>("/m/appointments", { query }),
    placeholderData: keepPreviousData,
    refetchInterval: usePollInterval(),
  });
  const types = useQuery({
    queryKey: ["appointments", "types"],
    queryFn: () => api<AppointmentType[]>("/m/appointments/types"),
  });
  const resources = useQuery({
    queryKey: ["appointments", "resources"],
    queryFn: () => api<Resource[]>("/m/appointments/resources"),
  });

  function set(values: Record<string, string>) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(values)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    setParams(next, { replace: true });
  }

  const days: string[] = [];
  for (let d = from; d <= to; d = addDays(d, 1)) days.push(d);
  const byDay = new Map<string, Appointment[]>();
  for (const a of list.data?.items ?? []) {
    const d = a.local.slice(0, 10);
    byDay.set(d, [...(byDay.get(d) ?? []), a]);
  }
  const step = view === "day" ? 1 : 7;

  return (
    <div>
      <PageTitle title="Appointments">
        {canEdit ? (
          <Button onClick={() => setCreating(true)}>
            <Plus /> New appointment
          </Button>
        ) : null}
      </PageTitle>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex items-center rounded-lg border border-line bg-surface">
          <Button variant="ghost" size="icon" aria-label="Earlier" onClick={() => set({ d: addDays(anchor, -step) })}>
            <ChevronLeft />
          </Button>
          <span className="min-w-36 text-center text-sm font-medium">
            {view === "day" ? dayLabel(from) : `${dayLabel(from)} – ${dayLabel(to)}`}
          </span>
          <Button variant="ghost" size="icon" aria-label="Later" onClick={() => set({ d: addDays(anchor, step) })}>
            <ChevronRight />
          </Button>
        </div>
        <Button variant="secondary" size="sm" onClick={() => set({ d: "" })} disabled={from <= today && today <= to}>
          Today
        </Button>
        <div className="flex rounded-lg border border-line p-0.5" role="group" aria-label="View">
          {(["day", "week"] as const).map((v) => (
            <button
              key={v}
              className={cn("rounded-md px-3 py-1 text-sm", view === v ? "bg-accent/10 font-medium text-accent-ink" : "text-ink-2")}
              aria-pressed={view === v}
              onClick={() => set({ v: v === "week" ? "" : v })}
            >
              {v === "day" ? "Day" : "Week"}
            </button>
          ))}
        </div>
      </div>
      <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <Select aria-label="Show" value={filter} onChange={(e) => set({ f: e.target.value === "active" ? "" : e.target.value })}>
          {FILTERS.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </Select>
        <Select aria-label="With" value={resource} onChange={(e) => set({ resource: e.target.value })}>
          <option value="">Everyone</option>
          {(resources.data ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </Select>
        <Select aria-label="Type" value={type} onChange={(e) => set({ type: e.target.value })}>
          <option value="">Every type</option>
          {(types.data ?? []).map((t) => (
            <option key={t.id} value={t.id}>
              {t.name_en}
            </option>
          ))}
        </Select>
      </div>

      <ErrorNote error={list.error} />
      {list.isPending ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : !list.data?.items.length ? (
        <Card>
          <Empty title={view === "day" ? "Nothing booked this day" : "Nothing booked this week"}>
            {!resources.data?.length ? (
              <>
                Add who can be booked and their hours in{" "}
                <Link className="text-accent-ink" to="/settings">
                  Settings
                </Link>
                .
              </>
            ) : null}
          </Empty>
        </Card>
      ) : (
        <div className={cn("space-y-4 transition-opacity", list.isPlaceholderData && list.isFetching && "opacity-60")}>
          {days
            .filter((d) => byDay.has(d))
            .map((d) => (
              <section key={d}>
                <h2 className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted">
                  {dayLabel(d)}
                  {d === today ? " · Today" : ""}
                </h2>
                <Card className="overflow-hidden">
                  <ul className="divide-y divide-line">
                    {byDay.get(d)!.map((a) => (
                      <li key={a.id}>
                        <button
                          className={cn(
                            "grid w-full grid-cols-[4.5rem_1fr_auto] items-center gap-x-3 px-4 py-3 text-left hover:bg-line/30",
                            a.status === "cancelled" && "opacity-60",
                          )}
                          onClick={() => setOpenId(a.id)}
                        >
                          <span className="tabular text-sm font-medium">
                            {hhmm(a.local)}
                            <span className="block text-xs font-normal text-muted">{plusMinutes(hhmm(a.local), a.type.duration_min)}</span>
                          </span>
                          <span className="min-w-0">
                            <span className="block truncate font-medium">{a.customer.name ?? contactHandle(a.customer)}</span>
                            <span className="block truncate text-xs text-muted">
                              {a.type.name_en} · {a.resource.name}
                              {a.subject_label ? ` · ${a.subject_label}` : ""}
                            </span>
                          </span>
                          <StatusBadge status={a.status} />
                        </button>
                      </li>
                    ))}
                  </ul>
                </Card>
              </section>
            ))}
        </div>
      )}

      <AppointmentDrawer id={openId} onClose={() => setOpenId(null)} resources={resources.data ?? []} />
      {creating ? (
        <NewAppointmentSheet
          types={(types.data ?? []).filter((t) => t.is_active)}
          resources={(resources.data ?? []).filter((r) => r.is_active)}
          onClose={() => setCreating(false)}
          onCreated={(a) => {
            setCreating(false);
            set({ d: a.local.slice(0, 10) });
            setOpenId(a.id);
          }}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- one appointment

function AppointmentDrawer({ id, onClose, resources }: { id: string | null; onClose: () => void; resources: Resource[] }) {
  const client = useQueryClient();
  const canEdit = useCan("agent");
  const noun = useContactNoun();
  const q = useQuery({
    queryKey: ["appointment", id],
    queryFn: () => api<AppointmentDetail>(`/m/appointments/${id}`),
    enabled: id !== null,
  });
  const [moving, setMoving] = useState<{
    date: string;
    time: string;
    resource_id: string;
  } | null>(null);
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<Appointment>(`/m/appointments/${id}`, { method: "PATCH", body }),
    onSuccess: () => {
      setMoving(null);
      void client.invalidateQueries({ queryKey: ["appointments"] });
      void client.invalidateQueries({ queryKey: ["appointment", id] });
      void client.invalidateQueries({ queryKey: ["today"] });
    },
  });
  const a = q.data;
  const live = a && (a.status === "requested" || a.status === "confirmed");

  return (
    <Sheet
      open={id !== null}
      onOpenChange={(v) => {
        if (!v) {
          setMoving(null);
          patch.reset();
          onClose();
        }
      }}
      wide
      title={a ? `${a.type.name_en} · ${dayLabel(a.local.slice(0, 10))}, ${hhmm(a.local)}` : "Appointment"}
      description={a ? `${a.ref} · ${a.customer.name ?? contactHandle(a.customer)}` : undefined}
    >
      {!a ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-6">
          <ErrorNote error={patch.error} />
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
            <dt className="text-muted">Status</dt>
            <dd>
              <StatusBadge status={a.status} />
            </dd>
            <dt className="text-muted">When</dt>
            <dd className="tabular">
              {dayLabel(a.local.slice(0, 10))}, {hhmm(a.local)}–{plusMinutes(hhmm(a.local), a.type.duration_min)}
            </dd>
            <dt className="text-muted">With</dt>
            <dd>{a.resource.name}</dd>
            <dt className="text-muted">Where</dt>
            <dd>
              {LOCATION_LABEL[a.type.location_kind]}
              {a.location_note ? ` · ${a.location_note}` : ""}
            </dd>
            {a.subject_label ? (
              <>
                <dt className="text-muted">About</dt>
                <dd>{a.subject_label}</dd>
              </>
            ) : null}
            <dt className="text-muted">{noun.charAt(0).toUpperCase() + noun.slice(1)}</dt>
            <dd>
              {a.customer.name ?? "—"} · {contactHandle(a.customer)}
            </dd>
            <dt className="text-muted">Booked by</dt>
            <dd>
              {a.source === "agent" ? "The agent" : "The team"}
              {a.conversation_id ? (
                <>
                  {" · "}
                  <Link className="text-accent-ink" to={`/conversations/${a.conversation_id}`}>
                    chat
                  </Link>
                </>
              ) : null}
            </dd>
            {a.notes ? (
              <>
                <dt className="text-muted">Notes</dt>
                <dd className="whitespace-pre-wrap" dir="auto">
                  {a.notes}
                </dd>
              </>
            ) : null}
          </dl>

          {canEdit && a.next_statuses.length ? (
            <div className="flex flex-wrap gap-2">
              {a.next_statuses.map((s) => (
                <Button
                  key={s}
                  size="sm"
                  variant={s === "cancelled" ? "danger" : s === "confirmed" ? "primary" : "secondary"}
                  disabled={patch.isPending}
                  onClick={() => {
                    if (s === "cancelled" && !window.confirm(`Cancel this appointment? The ${noun} is not messaged automatically.`)) return;
                    patch.mutate({ status: s });
                  }}
                >
                  {ACTION_LABEL[s]}
                </Button>
              ))}
              {live ? (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    setMoving({
                      date: a.local.slice(0, 10),
                      time: hhmm(a.local),
                      resource_id: a.resource.id,
                    })
                  }
                >
                  Move
                </Button>
              ) : null}
            </div>
          ) : null}

          {moving ? (
            <section className="space-y-3 rounded-lg border border-line p-3">
              <h3 className="text-sm font-medium">Move to</h3>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Date">
                  <Input type="date" value={moving.date} onChange={(e) => setMoving({ ...moving, date: e.target.value })} />
                </Field>
                <Field label="Time">
                  <Input type="time" step={300} value={moving.time} onChange={(e) => setMoving({ ...moving, time: e.target.value })} />
                </Field>
              </div>
              <Field label="With">
                <Select value={moving.resource_id} onChange={(e) => setMoving({ ...moving, resource_id: e.target.value })}>
                  {resources.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <p className="text-xs text-muted">The {noun} is not messaged automatically — let them know.</p>
              <div className="flex gap-2">
                <Button
                  disabled={patch.isPending || !moving.date || !moving.time}
                  onClick={() =>
                    patch.mutate({
                      starts_at: `${moving.date}T${moving.time}`,
                      ...(moving.resource_id !== a.resource.id ? { resource_id: moving.resource_id } : {}),
                    })
                  }
                >
                  Save
                </Button>
                <Button variant="ghost" onClick={() => setMoving(null)}>
                  Back
                </Button>
              </div>
            </section>
          ) : null}

          {a.history.length ? (
            <section>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">History</h3>
              <ul className="space-y-1 text-sm">
                {a.history.map((h, i) => (
                  <li key={i} className="flex justify-between gap-3">
                    <span>{describe(h)}</span>
                    <span className="shrink-0 text-xs text-muted">{dateTime(h.at)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </Sheet>
  );
}

function describe(h: AppointmentDetail["history"][number]): string {
  const who = h.actor === "agent" ? "Agent" : "Team";
  const after = h.after ?? {};
  if (h.action === "book_appointment") return `${who} booked it`;
  if (typeof after.status === "string") return `${who}: ${label(STATUS_LABEL, after.status)}`;
  if (typeof after.starts_at === "string") return `${who} moved it to ${after.starts_at.replace("T", " ")}`;
  return `${who}: ${h.action.replace(/_/g, " ")}`;
}

// ---------------------------------------------------------------- new appointment

function NewAppointmentSheet({
  types,
  resources,
  onClose,
  onCreated,
}: {
  types: AppointmentType[];
  resources: Resource[];
  onClose: () => void;
  onCreated: (a: Appointment) => void;
}) {
  const client = useQueryClient();
  const withListings = useHasModule("listings");
  const [contact, setContact] = useState<CustomerRow | CustomerDetail | null>(null);
  const [f, setF] = useState({
    type_id: types[0]?.id ?? "",
    date: addDays(localToday(), 1),
    time: "",
    resource_id: "",
    listing_ref: "",
    location_note: "",
    notes: "",
    needs_confirming: false,
  });
  const slots = useQuery({
    queryKey: ["appointments", "availability", f.type_id, f.date, f.resource_id],
    queryFn: () =>
      api<Slot[]>("/m/appointments/availability", {
        query: {
          type_id: f.type_id,
          from: f.date,
          to: f.date,
          resource_id: f.resource_id || undefined,
        },
      }),
    enabled: Boolean(f.type_id && f.date),
  });
  const save = useMutation({
    mutationFn: () =>
      api<Appointment>("/m/appointments", {
        method: "POST",
        body: {
          customer_id: contact!.id,
          type_id: f.type_id,
          starts_at: `${f.date}T${f.time}`,
          resource_id: f.resource_id || null,
          status: f.needs_confirming ? "requested" : "confirmed",
          location_note: f.location_note.trim() || null,
          notes: f.notes.trim() || null,
          subject: f.listing_ref.trim() ? { listing_ref: f.listing_ref.trim() } : null,
        },
      }),
    onSuccess: (a) => {
      void client.invalidateQueries({ queryKey: ["appointments"] });
      void client.invalidateQueries({ queryKey: ["today"] });
      onCreated(a);
    },
  });
  const offered = new Set((slots.data ?? []).map((s) => hhmm(s.local)));

  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title="New appointment"
      footer={
        <Button className="w-full" disabled={!contact || !f.type_id || !f.date || !f.time || save.isPending} onClick={() => save.mutate()}>
          Book {f.time ? `${dayLabel(f.date)}, ${f.time}` : ""}
        </Button>
      }
    >
      {!types.length || !resources.length ? (
        <Empty title="Nothing can be booked yet">
          Add appointment types and who can be booked in{" "}
          <Link className="text-accent-ink" to="/settings">
            Settings
          </Link>
          .
        </Empty>
      ) : (
        <div className="space-y-4">
          <ContactPicker value={contact} onChange={setContact} />
          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <Select value={f.type_id} onChange={(e) => setF({ ...f, type_id: e.target.value, time: "" })}>
                {types.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name_en} ({t.duration_min} min)
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="With">
              <Select value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value, time: "" })}>
                <option value="">Anyone free</option>
                {resources.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="Date">
            <Input type="date" min={localToday()} value={f.date} onChange={(e) => setF({ ...f, date: e.target.value, time: "" })} />
          </Field>
          <div>
            <p className="mb-1.5 text-sm font-medium text-ink-2">Free times</p>
            {slots.isPending ? (
              <Spinner />
            ) : slots.data?.length ? (
              <div className="flex flex-wrap gap-2" role="listbox" aria-label="Free times">
                {slots.data.map((s) => {
                  const t = hhmm(s.local);
                  return (
                    <button
                      key={s.starts_at}
                      role="option"
                      aria-selected={f.time === t}
                      className={cn(
                        "tabular rounded-lg border px-3 py-1.5 text-sm",
                        f.time === t ? "border-accent bg-accent/10 font-medium text-accent-ink" : "border-line hover:bg-line/40",
                      )}
                      onClick={() => setF({ ...f, time: t })}
                    >
                      {t}
                    </button>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-muted">No free times this day.</p>
            )}
            <Field label="Or another time" hint="Outside the usual hours is fine; a clash with another booking is not." className="mt-3">
              <Input
                type="time"
                step={300}
                value={offered.has(f.time) ? "" : f.time}
                onChange={(e) => setF({ ...f, time: e.target.value })}
              />
            </Field>
          </div>
          {withListings ? (
            <Field label="Property ref" hint="For a viewing: the listing's reference. Its address becomes the place.">
              <Input value={f.listing_ref} onChange={(e) => setF({ ...f, listing_ref: e.target.value })} />
            </Field>
          ) : null}
          <Field label="Place" hint="Optional: an address or a meeting link">
            <Input value={f.location_note} onChange={(e) => setF({ ...f, location_note: e.target.value })} />
          </Field>
          <Field label="Notes">
            <Textarea value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="size-4"
              checked={f.needs_confirming}
              onChange={(e) => setF({ ...f, needs_confirming: e.target.checked })}
            />
            Pencil it in (confirm later)
          </label>
          <ErrorNote error={save.error} />
        </div>
      )}
    </Sheet>
  );
}
