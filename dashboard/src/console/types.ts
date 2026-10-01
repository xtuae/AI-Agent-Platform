import type { Health, Statement } from "@/lib/types";

export interface ConsoleUsage {
  msgs_in: number;
  msgs_out: number;
  meta_cost_aed: string;
  borne_aed: string;
  llm_prompt_tokens: number;
  llm_completion_tokens: number;
  llm_cost_usd: string;
  llm_cost_aed: string;
}

export interface TenantRow {
  id: string;
  name: string;
  slug: string;
  status: string;
  modules: string[];
  channels: {
    display_phone: string | null;
    quality_rating: string | null;
    messaging_limit_tier: string | null;
    token_expires_at: string | null;
    is_active: boolean;
  }[];
  usage: ConsoleUsage;
  cap_aed: string | null;
  pct_of_cap: number | null;
  margin: {
    fee_aed: string | null;
    revenue_aed: string | null;
    direct_cost_aed: string;
    margin_aed: string | null;
    margin_pct: number | null;
  };
  health: {
    status: Health;
    problems: string[];
    last_inbound_at: string | null;
    awaiting_human: number;
    stuck: number;
    failed_24h: number;
  };
  statement: string;
  free_until: string | null;
  borne_until: string | null;
}

export interface Overview {
  month: string;
  platform: { database: boolean; redis: boolean; worker: boolean | null; queue_depth: number | null };
  tenants: TenantRow[];
  totals: ConsoleUsage;
  revenue_aed: string;
  direct_cost_aed: string;
}

export interface LedgerRow {
  tenant_id: string;
  tenant_name: string;
  service_month: number;
  start: string;
  end: string;
  metered_aed: string;
  statement_aed: string | null;
  payable_aed: string;
  basis: "meta_statement" | "metered";
  state: "upcoming" | "running" | "due" | "paid";
  paid_aed: string | null;
  paid_at: string | null;
  reference: string | null;
}

export interface TenantDetail {
  tenant: TenantRow;
  days: { day: string; msgs_in: number; msgs_out: number; counts: Record<string, number>; meta_cost_aed: string; llm_cost_usd: string }[];
  statement: Statement;
  ledger: LedgerRow[];
}
