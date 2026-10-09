// Display helpers. Money arrives as decimal strings from the server and is only ever formatted
// here — the dashboard never does arithmetic on prices (the one exception is the new-order
// preview, which is labelled as an estimate and replaced by the server's total on save).

let tz = "Asia/Dubai";
export function setTimezone(value: string): void {
  tz = value;
}

export function aed(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return `AED ${n.toLocaleString("en-AE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function compact(n: number): string {
  return n.toLocaleString("en", { notation: n >= 10_000 ? "compact" : "standard" });
}

export function phone(waId: string): string {
  // 971501234567 → +971 50 123 4567 (UAE); anything else → +<digits>
  const m = /^971(5\d)(\d{3})(\d{4})$/.exec(waId);
  return m ? `+971 ${m[1]} ${m[2]} ${m[3]}` : `+${waId}`;
}

/** How to show who a customer is when there may be no number: the number, else the Telegram
 *  handle, else "Telegram". */
export function contactHandle(c: { wa_id: string | null; channels?: { kind: string; handle: string | null }[] }): string {
  if (c.wa_id) return phone(c.wa_id);
  const tg = c.channels?.find((x) => x.kind === "telegram");
  return tg?.handle ?? "Telegram";
}

export function dateLabel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = iso.length === 10 ? new Date(`${iso}T12:00:00Z`) : new Date(iso);
  return d.toLocaleDateString("en-GB", {
    timeZone: iso.length === 10 ? "UTC" : tz,
    day: "numeric",
    month: "short",
  });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", {
    timeZone: tz,
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function timeOnly(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return dateLabel(iso);
}

/** Today's date in the tenant's timezone, YYYY-MM-DD. */
export function localToday(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date());
}

export const STATUS_LABEL: Record<string, string> = {
  draft: "Draft",
  confirmed: "Confirmed",
  out_for_delivery: "Out for delivery",
  delivered: "Delivered",
  cancelled: "Cancelled",
  open: "Agent",
  awaiting_human: "Needs a person",
  closed: "Closed",
  pending: "Not yet",
  opted_in: "Opted in",
  opted_out: "Opted out",
  requested: "Requested",
  completed: "Completed",
  no_show: "No-show",
  available: "Available",
  under_offer: "Under offer",
  let: "Let",
  sold: "Sold",
  archived: "Archived",
};

export const ESCALATION_LABEL: Record<string, string> = {
  complaint: "Complaint",
  refund_or_billing: "Refund / billing",
  angry_customer: "Upset customer",
  damaged_goods: "Damaged goods",
  driver_issue: "Driver issue",
  out_of_scope: "Out of scope",
  agent_unsure: "Agent unsure",
  customer_asked_for_human: "Asked for a person",
  taken_over: "Taken over",
};

export function label(map: Record<string, string>, key: string | null | undefined): string {
  if (!key) return "—";
  return map[key] ?? key.replace(/_/g, " ");
}
