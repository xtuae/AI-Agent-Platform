// Response shapes of /api/v1 (mirrors the Pydantic models in api/api/v1/*).

export type OrderStatus = "draft" | "confirmed" | "out_for_delivery" | "delivered" | "cancelled";
export type Health = "ok" | "degraded" | "down";

export interface OrderLine {
  sku: string;
  name: string | null;
  qty: number;
  unit_price_aed: string | null;
  line_total_aed: string | null;
  paid_with_coupon: boolean;
}

/** A channel a customer is on, with their handle there (api/api/v1/common.py ChannelRef). */
export interface ChannelRef {
  kind: "whatsapp" | "telegram";
  name: string;
  /** WhatsApp: the number's digits; Telegram: "@username" or their display name. */
  handle: string | null;
  /** They blocked the bot: unreachable until they write again. */
  blocked: boolean;
}

export interface CustomerRef {
  id: string;
  name: string | null;
  /** null for a customer known only on Telegram. */
  wa_id: string | null;
  area: string | null;
  address_note?: string | null;
}

export interface Order {
  id: string;
  order_no: string;
  status: OrderStatus;
  items: OrderLine[];
  total_aed: string | null;
  source: string | null;
  area: string | null;
  delivery_date: string | null;
  delivery_slot: string | null;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  conversation_id: string | null;
  customer: CustomerRef;
  next_statuses: OrderStatus[];
}

export interface OrderDetail extends Order {
  history: {
    at: string;
    actor: string;
    action: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
  }[];
}

export interface Page<T> {
  items: T[];
  total: number;
}

export interface DeliveryList {
  date: string;
  unscheduled: number;
  groups: {
    area: string;
    bottles: number;
    to_collect_aed: string;
    stops: {
      order_no: string;
      status: OrderStatus;
      customer_name: string | null;
      wa_id: string | null;
      address_note: string | null;
      delivery_slot: string | null;
      items: OrderLine[];
      total_aed: string | null;
      notes: string | null;
    }[];
  }[];
}

export interface Today {
  date: string;
  live_conversations: number;
  awaiting_human: number;
  spend: {
    month: string;
    meta_cost_aed: string;
    cap_aed: string | null;
    pct_of_cap: number | null;
    messages_out: number;
    borne_by_hmh: boolean;
    borne_until: string | null;
  };
  health: {
    status: Health;
    last_agent_reply_at: string | null;
    checks: { name: string; status: Health; detail: string }[];
  };
  spend_by_day: { day: string; cumulative_aed: string }[];
  /** Each enabled module's Today data, keyed by module key (shape owned by the module). */
  modules: Record<string, unknown>;
}

export type ConvState = "open" | "awaiting_human" | "closed";

export interface ConversationRow {
  id: string;
  state: ConvState;
  customer: CustomerRef;
  /** The channel this conversation is on. */
  channel: ChannelRef;
  assigned_to: string | null;
  assigned_to_name: string | null;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  /** Always true on a channel without a service window (Telegram). */
  window_open: boolean;
  window_expires_at: string | null;
  language: string | null;
  last_message: { direction: "in" | "out"; msg_type: string | null; preview: string | null; at: string } | null;
  escalation: { reason: string | null; summary: string | null; urgency: string | null; at: string; actor: string } | null;
  waiting: number;
}

export interface MessageOut {
  id: string;
  direction: "in" | "out";
  msg_type: string | null;
  body: string | null;
  transcript: string | null;
  template_name: string | null;
  status: string | null;
  error_code: string | null;
  author: "customer" | "agent" | "person" | "template";
  sent_by_name: string | null;
  created_at: string;
}

export interface Thread extends ConversationRow {
  summary: string | null;
  messages: MessageOut[];
  has_more: boolean;
}

export type OptIn = "pending" | "opted_in" | "opted_out";

export interface CustomerRow {
  id: string;
  /** null for a customer known only on Telegram. */
  wa_id: string | null;
  name: string | null;
  area: string | null;
  emirate: string | null;
  language: string | null;
  source: string | null;
  opt_in_status: OptIn;
  channels: ChannelRef[];
}

export interface CustomerDetail extends CustomerRow {
  address_note: string | null;
  external_ref: string | null;
  opt_in_at: string | null;
  opt_in_evidence: Record<string, unknown> | null;
  opt_out_at: string | null;
  conversations: { id: string; state: ConvState; last_inbound_at: string | null }[];
  /** One panel per enabled module that has one, keyed by module key. */
  modules: Record<string, unknown>;
}

export interface Product {
  id: string;
  sku: string;
  name_en: string | null;
  name_ar: string | null;
  category: string | null;
  brand: string | null;
  price_aed: string | null;
  is_active: boolean;
  cross_sell_priority: number | null;
  stock_note: string | null;
}

export interface Settings {
  business_name: string;
  timezone: string;
  business_hours: Record<string, string>;
  escalation_phone: string | null;
  monthly_message_cap_aed: string | null;
  meta_charges_borne_by_us_until: string | null;
}

export interface TeamMember {
  id: string;
  email: string;
  name: string | null;
  role: "viewer" | "agent" | "admin";
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

// ---- Costs (Phase 5)

export type PricingCategory = "marketing" | "utility" | "service" | "authentication";
export type ReconStatus = "ok" | "check" | "not_pulled" | "not_comparable";

export interface ReconLine {
  key: string; // category, ISO day, or "total"
  ours_count: number;
  meta_count: number | null;
  ours_aed: string;
  meta_aed: string | null;
  diff_aed: string | null;
  status: ReconStatus;
}

export interface Statement {
  pulled_at: string | null;
  complete: boolean;
  currency: string | null;
  meta_total_native: string | null;
  total: ReconLine | null;
  categories: ReconLine[];
  days: ReconLine[];
}

export interface Costs {
  month: string;
  months: string[];
  cap_aed: string | null;
  total_aed: string;
  pct_of_cap: number | null;
  borne_aed: string;
  due_aed: string;
  borne_until: string | null;
  categories: { category: PricingCategory; count: number; cost_aed: string }[];
  days: {
    day: string;
    msgs_in: number;
    msgs_out: number;
    counts: Record<PricingCategory, number>;
    costs: Record<PricingCategory, string>;
    total_aed: string;
    borne_by_hmh: boolean;
  }[];
  statement: Statement;
}

// ---- Opt-in consent pages (Phase 5)

export interface OptinLink {
  id: string;
  code: string;
  url: string;
  label: string;
  source: string;
  language: "en" | "ar";
  heading: string;
  wording: string;
  prefill: string;
  is_active: boolean;
  created_at: string;
  visits: number;
  opted_in: number;
}
