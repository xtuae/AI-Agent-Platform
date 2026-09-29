// Response shapes of /api/v1/m/appointments (mirrors api/modules/appointments/routes.py).

export type AppointmentStatus = "requested" | "confirmed" | "cancelled" | "completed" | "no_show";
export type LocationKind = "office" | "onsite" | "video" | "phone";

export interface AppointmentType {
  id: string;
  name_en: string;
  name_ar: string | null;
  duration_min: number;
  location_kind: LocationKind;
  needs_address: boolean;
  is_active: boolean;
}

export interface Resource {
  id: string;
  name: string;
  tenant_user_id: string | null;
  is_active: boolean;
}

export interface Appointment {
  id: string;
  ref: string;
  status: AppointmentStatus;
  starts_at: string;
  ends_at: string;
  /** Tenant-local "YYYY-MM-DDTHH:MM". */
  local: string;
  type: AppointmentType;
  resource: Resource;
  customer: { id: string; name: string | null; wa_id: string };
  subject_module: string | null;
  subject_label: string | null;
  location_note: string | null;
  notes: string | null;
  source: string;
  created_by: string | null;
  created_at: string;
  conversation_id: string | null;
  next_statuses: AppointmentStatus[];
}

export interface AppointmentDetail extends Appointment {
  history: {
    at: string;
    actor: string;
    action: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
  }[];
}

export interface Slot {
  starts_at: string;
  local: string;
  resource_ids: string[];
}

export interface Hours {
  weekday: number; // 0 = Monday
  start_time: string; // "09:00:00"
  end_time: string;
}

export interface TimeOff {
  id: string;
  resource_id: string | null;
  day: string;
  start_time: string | null;
  end_time: string | null;
  reason: string | null;
}

/** One row in today.modules.appointments.items and the contact panel. */
export interface AgendaRow {
  id: string;
  ref: string;
  type: string;
  resource: string;
  starts_at: string;
  local: string;
  duration_min: number;
  status: AppointmentStatus;
  about: string | null;
  customer?: string | null;
}

/** today.modules.appointments */
export interface AppointmentsToday {
  count: number;
  next: AgendaRow | null;
  awaiting_confirmation: number;
  items: AgendaRow[];
}

/** contact.modules.appointments */
export interface AppointmentsPanel {
  upcoming: AgendaRow[];
  past: AgendaRow[];
}
