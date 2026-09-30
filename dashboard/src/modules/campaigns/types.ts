// Response shapes of /api/v1/m/campaigns (mirrors api/modules/campaigns/routes.py).

export type CampaignStatus = "draft" | "approved" | "sending" | "paused" | "done" | "cancelled";

export interface Variable {
  index: number;
  meaning: string;
  example: string;
}

export interface Check {
  errors: string[];
  warnings: string[];
}

export interface Template {
  id: string;
  name: string;
  language: string;
  category: string | null;
  body: string | null;
  variables: Variable[];
  meta_status: string | null;
  rejected_reason: string | null;
  source: "drafted" | "meta";
  submitted_at: string | null;
  created_at: string;
  check: Check;
}

export interface Draft {
  name: string;
  category: "MARKETING" | "UTILITY";
  language: "en" | "ar";
  body: string;
  variables: Variable[];
  rationale: string;
  check: Check;
}

export interface Binding {
  source: "contact.first_name" | "contact.name" | "contact.area" | "text";
  value?: string | null;
  fallback?: string | null;
}

export interface SegmentField {
  name: string;
  label: string;
  kind: "int" | "text" | "text_list";
  help: string;
  minimum: number;
  maximum: number;
  choices: string[];
}

export interface Campaign {
  id: string;
  name: string;
  status: CampaignStatus;
  template_id: string | null;
  template_name: string | null;
  segment: Record<string, unknown> | null;
  variable_bindings: Binding[];
  budget_cap_aed: string | null;
  throttle_per_minute: number;
  approved_by: string | null;
  approved_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  paused_reason: string | null;
  created_at: string;
  recipient_count: number;
  sent_count: number;
  delivered_count: number;
  read_count: number;
  reply_count: number;
  skipped_count: number;
  failed_count: number;
  spend_aed: string;
}

export interface CampaignDetail extends Campaign {
  results: {
    by_status: Record<string, number>;
    skip_reasons: Record<string, number>;
    modules: Record<string, Record<string, unknown>>;
  };
}

export interface Preview {
  recipients: number;
  estimated_cost_aed: string | null;
  sample: string | null;
  sample_to: string | null;
  problems: string[];
}

export interface Guardrails {
  quality_rating: string | null;
  messaging_limit_tier: string | null;
  quality_block: string | null;
  may_send_now: boolean;
  next_send_time: string | null;
  frequency_days: number;
  sent_last_24h: number;
  tier_limit: number | null;
}

/** today.modules.campaigns */
export interface CampaignsToday {
  sending: number;
  approved: number;
  paused: { name: string; reason: string | null }[];
  quality_rating: string | null;
  quality_block: string | null;
}

/** contact.modules.campaigns */
export interface CampaignsPanel {
  received: { campaign: string; status: string | null; skip_reason: string | null; sent_at: string | null; replied_at: string | null }[];
}
