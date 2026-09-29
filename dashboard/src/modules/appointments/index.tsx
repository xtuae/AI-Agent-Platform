import { CalendarDays } from "lucide-react";
import { lazy } from "react";
import { Link } from "react-router";
import { Card, CardHeader } from "@/components/ui/card";
import { Tile } from "@/components/ui/tile";
import type { ModuleDef } from "../types";
import { dayLabel, hhmm, plusMinutes, StatusBadge } from "./shared";
import type { AgendaRow, AppointmentsPanel, AppointmentsToday } from "./types";

function TodayTiles({ data }: { data: unknown }) {
  const d = data as AppointmentsToday;
  return (
    <>
      <Tile
        label="Appointments today"
        value={String(d.count)}
        sub={d.next ? `Next ${hhmm(d.next.local)} · ${d.next.customer ?? d.next.type}` : "Nothing else today"}
        to="/appointments"
      />
      {d.awaiting_confirmation > 0 ? (
        <Tile
          label="To confirm"
          value={String(d.awaiting_confirmation)}
          sub="Booked by the agent, waiting for the team"
          to="/appointments?f=requested"
          emphasis
        />
      ) : null}
    </>
  );
}

function Row({ a, withDay, withCustomer }: { a: AgendaRow; withDay?: boolean; withCustomer?: boolean }) {
  return (
    <li className="flex items-center justify-between gap-3 px-3 py-2">
      <div className="min-w-0">
        <p className="truncate">
          <span className="tabular font-medium">
            {withDay ? `${dayLabel(a.local.slice(0, 10))}, ` : ""}
            {hhmm(a.local)}–{plusMinutes(hhmm(a.local), a.duration_min)}
          </span>{" "}
          {withCustomer && a.customer ? a.customer : a.type}
        </p>
        <p className="truncate text-xs text-muted">
          {withCustomer ? `${a.type} · ` : ""}
          {a.resource}
          {a.about ? ` · ${a.about}` : ""}
        </p>
      </div>
      <StatusBadge status={a.status} />
    </li>
  );
}

/** Today's agenda, in the chart slot. */
function TodayChart({ data }: { data: unknown }) {
  const d = data as AppointmentsToday;
  return (
    <Card>
      <CardHeader
        title="Today's appointments"
        action={
          <Link to="/appointments" className="text-sm font-medium text-accent-ink underline-offset-2 hover:underline">
            Agenda
          </Link>
        }
      />
      {d.items.length ? (
        <ul className="mt-3 divide-y divide-line border-t border-line text-sm">
          {d.items.map((a) => (
            <Row key={a.id} a={a} withCustomer />
          ))}
        </ul>
      ) : (
        <p className="px-4 pb-4 pt-2 text-sm text-muted">No appointments today.</p>
      )}
    </Card>
  );
}

function ContactPanel({ data }: { data: unknown }) {
  const d = data as AppointmentsPanel;
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Appointments</h3>
      {d.upcoming.length || d.past.length ? (
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {[...d.upcoming, ...d.past.slice(0, 10)].map((a) => (
            <Row key={a.id} a={a} withDay />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No appointments yet.</p>
      )}
    </section>
  );
}

export const appointments: ModuleDef = {
  key: "appointments",
  nav: [{ to: "/appointments", label: "Appointments", icon: CalendarDays }],
  routes: [{ path: "appointments", element: lazy(() => import("./AppointmentsPage")) }],
  TodayTiles,
  TodayChart,
  ContactPanel,
  SettingsSection: lazy(() => import("./AppointmentsSettings")),
};
