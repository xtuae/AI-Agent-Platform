import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import type { TeamMember } from "@/lib/types";
import { dayLabel, LOCATION_LABEL, WEEKDAYS } from "./shared";
import type { AppointmentType, Hours, LocationKind, Resource, TimeOff } from "./types";

const DURATIONS = [15, 20, 30, 45, 60, 90, 120, 180, 240];

export default function AppointmentsSettings() {
  return (
    <>
      <TypesCard />
      <ResourcesCard />
      <TimeOffCard />
    </>
  );
}

// ---------------------------------------------------------------- types

function TypesCard() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const types = useQuery({
    queryKey: ["appointments", "types"],
    queryFn: () => api<AppointmentType[]>("/m/appointments/types"),
  });
  const [editing, setEditing] = useState<AppointmentType | "new" | null>(null);
  return (
    <Card>
      <CardHeader
        title="Appointment types"
        subtitle="What customers can book. The agent offers only these."
        action={
          isAdmin ? (
            <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
              <Plus /> Add
            </Button>
          ) : null
        }
      />
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {(types.data ?? []).map((t) => (
          <li key={t.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{t.name_en}</p>
              <p className="text-xs text-muted">
                {t.duration_min} min · {LOCATION_LABEL[t.location_kind]}
                {t.needs_address ? " · needs an address" : ""}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {!t.is_active ? <Badge>Hidden</Badge> : null}
              {isAdmin ? (
                <Button size="sm" variant="ghost" onClick={() => setEditing(t)}>
                  Edit
                </Button>
              ) : null}
            </div>
          </li>
        ))}
        {types.data && !types.data.length ? <li className="px-4 py-3 text-sm text-muted">None yet.</li> : null}
      </ul>
      {types.isPending ? (
        <div className="p-4">
          <Spinner />
        </div>
      ) : null}
      {editing ? (
        <TypeSheet
          type={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void client.invalidateQueries({ queryKey: ["appointments"] });
          }}
        />
      ) : null}
    </Card>
  );
}

function TypeSheet({ type, onClose, onSaved }: { type: AppointmentType | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    name_en: type?.name_en ?? "",
    name_ar: type?.name_ar ?? "",
    duration_min: type?.duration_min ?? 30,
    location_kind: (type?.location_kind ?? "office") as LocationKind,
    needs_address: type?.needs_address ?? false,
    is_active: type?.is_active ?? true,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = { ...f, name_ar: f.name_ar || null };
      return type
        ? api<AppointmentType>(`/m/appointments/types/${type.id}`, {
            method: "PATCH",
            body,
          })
        : api<AppointmentType>("/m/appointments/types", {
            method: "POST",
            body,
          });
    },
    onSuccess: onSaved,
  });
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      title={type ? `Edit ${type.name_en}` : "Add appointment type"}
      footer={
        <Button className="w-full" disabled={save.isPending || !f.name_en.trim()} onClick={() => save.mutate()}>
          Save
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Name (English)" hint="e.g. Property viewing, Initial consultation">
          <Input value={f.name_en} onChange={(e) => setF({ ...f, name_en: e.target.value })} />
        </Field>
        <Field label="Name (Arabic)">
          <Input dir="rtl" value={f.name_ar} onChange={(e) => setF({ ...f, name_ar: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Length">
            <Select value={f.duration_min} onChange={(e) => setF({ ...f, duration_min: Number(e.target.value) })}>
              {DURATIONS.map((d) => (
                <option key={d} value={d}>
                  {d < 60 ? `${d} min` : `${d / 60} h`}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Where">
            <Select value={f.location_kind} onChange={(e) => setF({ ...f, location_kind: e.target.value as LocationKind })}>
              {(Object.keys(LOCATION_LABEL) as LocationKind[]).map((k) => (
                <option key={k} value={k}>
                  {LOCATION_LABEL[k]}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="size-4"
            checked={f.needs_address}
            onChange={(e) => setF({ ...f, needs_address: e.target.checked })}
          />
          Needs the customer's address (a visit to them)
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="size-4" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} />
          Can be booked
        </label>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}

// ---------------------------------------------------------------- resources + weekly hours

function ResourcesCard() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const resources = useQuery({
    queryKey: ["appointments", "resources"],
    queryFn: () => api<Resource[]>("/m/appointments/resources"),
  });
  const [editing, setEditing] = useState<Resource | "new" | null>(null);
  return (
    <Card>
      <CardHeader
        title="Who can be booked"
        subtitle="People or rooms, each with weekly hours. A slot is free if anyone is."
        action={
          isAdmin ? (
            <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
              <Plus /> Add
            </Button>
          ) : null
        }
      />
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {(resources.data ?? []).map((r) => (
          <li key={r.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <p className="truncate text-sm font-medium">{r.name}</p>
            <div className="flex shrink-0 items-center gap-2">
              {!r.is_active ? <Badge>Off</Badge> : null}
              {isAdmin ? (
                <Button size="sm" variant="ghost" onClick={() => setEditing(r)}>
                  Edit hours
                </Button>
              ) : null}
            </div>
          </li>
        ))}
        {resources.data && !resources.data.length ? <li className="px-4 py-3 text-sm text-muted">None yet.</li> : null}
      </ul>
      {editing ? (
        <ResourceSheet
          resource={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void client.invalidateQueries({ queryKey: ["appointments"] });
          }}
        />
      ) : null}
    </Card>
  );
}

type Range = { start: string; end: string };

function toWeek(rows: Hours[]): Range[][] {
  const week: Range[][] = WEEKDAYS.map(() => []);
  for (const h of rows)
    week[h.weekday]?.push({
      start: h.start_time.slice(0, 5),
      end: h.end_time.slice(0, 5),
    });
  return week;
}

function ResourceSheet({ resource, onClose, onSaved }: { resource: Resource | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    name: resource?.name ?? "",
    tenant_user_id: resource?.tenant_user_id ?? "",
    is_active: resource?.is_active ?? true,
  });
  const team = useQuery({
    queryKey: ["team"],
    queryFn: () => api<TeamMember[]>("/team"),
  });
  const hours = useQuery({
    queryKey: ["appointments", "hours", resource?.id],
    queryFn: () => api<Hours[]>(`/m/appointments/resources/${resource!.id}/hours`),
    enabled: resource !== null,
  });
  const [week, setWeek] = useState<Range[][] | null>(null);
  const shown =
    week ??
    (resource
      ? hours.data
        ? toWeek(hours.data)
        : null
      : toWeek(
          [0, 1, 2, 3, 4].map((d) => ({
            weekday: d,
            start_time: "09:00",
            end_time: "17:00",
          })),
        ));

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: f.name.trim(),
        tenant_user_id: f.tenant_user_id || null,
        is_active: f.is_active,
      };
      const saved = resource
        ? await api<Resource>(`/m/appointments/resources/${resource.id}`, {
            method: "PATCH",
            body,
          })
        : await api<Resource>("/m/appointments/resources", {
            method: "POST",
            body,
          });
      if (shown) {
        await api<Hours[]>(`/m/appointments/resources/${saved.id}/hours`, {
          method: "PUT",
          body: {
            hours: shown.flatMap((ranges, weekday) =>
              ranges
                .filter((r) => r.start && r.end)
                .map((r) => ({
                  weekday,
                  start_time: r.start,
                  end_time: r.end,
                })),
            ),
          },
        });
      }
      return saved;
    },
    onSuccess: onSaved,
  });

  function setDay(day: number, ranges: Range[]) {
    const next = (shown ?? WEEKDAYS.map(() => [])).map((r) => [...r]);
    next[day] = ranges;
    setWeek(next);
  }

  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title={resource ? resource.name : "Add who can be booked"}
      footer={
        <Button className="w-full" disabled={save.isPending || !f.name.trim() || !shown} onClick={() => save.mutate()}>
          Save
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Name" hint="A person (Sara, Omar) or a room">
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        </Field>
        <Field label="Team member" hint="Optional: link to their dashboard login">
          <Select value={f.tenant_user_id} onChange={(e) => setF({ ...f, tenant_user_id: e.target.value })}>
            <option value="">Not linked</option>
            {(team.data ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.name ?? m.email}
              </option>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="size-4" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} />
          Taking bookings
        </label>

        <section>
          <h3 className="mb-2 text-sm font-medium text-ink-2">Weekly hours</h3>
          {!shown ? (
            <Spinner />
          ) : (
            <ul className="divide-y divide-line rounded-lg border border-line">
              {WEEKDAYS.map((name, day) => {
                const ranges = shown[day] ?? [];
                return (
                  <li key={name} className="grid grid-cols-1 items-start gap-1 px-3 py-2 sm:grid-cols-[6.5rem_1fr] sm:gap-2">
                    <span className="text-sm font-medium sm:pt-2 sm:font-normal">{name}</span>
                    <div className="space-y-2">
                      {ranges.length ? null : <p className="text-sm text-muted sm:pt-2">Closed</p>}
                      {ranges.map((r, i) => (
                        <div key={i} className="flex items-center gap-1.5">
                          <Input
                            type="time"
                            step={900}
                            className="min-w-0 flex-1"
                            aria-label={`${name} from`}
                            value={r.start}
                            onChange={(e) =>
                              setDay(
                                day,
                                ranges.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)),
                              )
                            }
                          />
                          <span className="text-muted">–</span>
                          <Input
                            type="time"
                            step={900}
                            className="min-w-0 flex-1"
                            aria-label={`${name} to`}
                            value={r.end}
                            onChange={(e) =>
                              setDay(
                                day,
                                ranges.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)),
                              )
                            }
                          />
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Remove ${name} hours`}
                            onClick={() =>
                              setDay(
                                day,
                                ranges.filter((_, j) => j !== i),
                              )
                            }
                          >
                            <X />
                          </Button>
                        </div>
                      ))}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          const last = ranges[ranges.length - 1];
                          setDay(day, [...ranges, last ? { start: last.end, end: "" } : { start: "09:00", end: "17:00" }]);
                        }}
                      >
                        <Plus /> {ranges.length ? "Add hours" : "Open"}
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}

// ---------------------------------------------------------------- time off

function TimeOffCard() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const list = useQuery({
    queryKey: ["appointments", "exceptions"],
    queryFn: () => api<TimeOff[]>("/m/appointments/exceptions"),
  });
  const resources = useQuery({
    queryKey: ["appointments", "resources"],
    queryFn: () => api<Resource[]>("/m/appointments/resources"),
  });
  const [f, setF] = useState<{
    day: string;
    resource_id: string;
    whole: boolean;
    start: string;
    end: string;
    reason: string;
  } | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: ["appointments"] });
  const add = useMutation({
    mutationFn: () =>
      api<TimeOff>("/m/appointments/exceptions", {
        method: "POST",
        body: {
          day: f!.day,
          resource_id: f!.resource_id || null,
          start_time: f!.whole ? null : f!.start,
          end_time: f!.whole ? null : f!.end,
          reason: f!.reason.trim() || null,
        },
      }),
    onSuccess: () => {
      setF(null);
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api<void>(`/m/appointments/exceptions/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });
  const nameOf = (id: string | null) => (id ? (resources.data?.find((r) => r.id === id)?.name ?? "—") : "Everyone");

  return (
    <Card>
      <CardHeader
        title="Time off"
        subtitle="Holidays and leave. Nothing is offered in this time."
        action={
          isAdmin && !f ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() =>
                setF({
                  day: "",
                  resource_id: "",
                  whole: true,
                  start: "09:00",
                  end: "13:00",
                  reason: "",
                })
              }
            >
              <Plus /> Add
            </Button>
          ) : null
        }
      />
      {f ? (
        <div className="mx-4 mt-3 space-y-3 rounded-lg border border-line p-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Date">
              <Input type="date" value={f.day} onChange={(e) => setF({ ...f, day: e.target.value })} />
            </Field>
            <Field label="Who">
              <Select value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value })}>
                <option value="">Everyone</option>
                {(resources.data ?? []).map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className="size-4" checked={f.whole} onChange={(e) => setF({ ...f, whole: e.target.checked })} />
            The whole day
          </label>
          {!f.whole ? (
            <div className="grid grid-cols-2 gap-3">
              <Field label="From">
                <Input type="time" value={f.start} onChange={(e) => setF({ ...f, start: e.target.value })} />
              </Field>
              <Field label="To">
                <Input type="time" value={f.end} onChange={(e) => setF({ ...f, end: e.target.value })} />
              </Field>
            </div>
          ) : null}
          <Field label="Reason" hint="Optional, only the team sees it">
            <Input value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
          </Field>
          <ErrorNote error={add.error} />
          <div className="flex gap-2">
            <Button disabled={!f.day || add.isPending} onClick={() => add.mutate()}>
              Save
            </Button>
            <Button variant="ghost" onClick={() => setF(null)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {(list.data ?? []).map((e) => (
          <li key={e.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
            <div className="min-w-0">
              <p className="font-medium">
                {dayLabel(e.day)}
                {e.start_time && e.end_time ? `, ${e.start_time.slice(0, 5)}–${e.end_time.slice(0, 5)}` : ", all day"}
              </p>
              <p className="truncate text-xs text-muted">
                {nameOf(e.resource_id)}
                {e.reason ? ` · ${e.reason}` : ""}
              </p>
            </div>
            {isAdmin ? (
              <Button size="icon" variant="ghost" aria-label="Remove" disabled={remove.isPending} onClick={() => remove.mutate(e.id)}>
                <Trash2 />
              </Button>
            ) : null}
          </li>
        ))}
        {list.data && !list.data.length ? <li className="px-4 py-3 text-sm text-muted">No time off coming up.</li> : null}
      </ul>
    </Card>
  );
}
