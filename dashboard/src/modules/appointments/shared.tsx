import { Badge } from "@/components/ui/badge";
import { label, STATUS_LABEL } from "@/lib/format";
import type { AppointmentStatus, LocationKind } from "./types";

const TONE: Record<AppointmentStatus, "neutral" | "accent" | "good" | "warn" | "bad"> = {
  requested: "warn",
  confirmed: "accent",
  completed: "good",
  cancelled: "neutral",
  no_show: "bad",
};

export function StatusBadge({ status }: { status: AppointmentStatus }) {
  return <Badge tone={TONE[status]}>{label(STATUS_LABEL, status)}</Badge>;
}

export const LOCATION_LABEL: Record<LocationKind, string> = {
  office: "At the office",
  onsite: "On site",
  video: "Video call",
  phone: "Phone call",
};

export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** "2030-01-08T10:00" → "10:00" */
export function hhmm(local: string): string {
  return local.slice(11, 16);
}

/** "10:00" + 45 min → "10:45" (tenant-local wall clock; no timezone maths needed). */
export function plusMinutes(time: string, minutes: number): string {
  const [h = 0, m = 0] = time.split(":").map(Number);
  const t = (h * 60 + m + minutes) % (24 * 60);
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

/** "2030-01-08" → "Tue 8 Jan" */
export function dayLabel(iso: string): string {
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

export function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** Monday of the week containing `iso`. */
export function weekStart(iso: string): string {
  const day = (new Date(`${iso}T12:00:00Z`).getUTCDay() + 6) % 7;
  return addDays(iso, -day);
}
