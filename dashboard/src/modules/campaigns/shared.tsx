import { Badge } from "@/components/ui/badge";
import type { CampaignStatus } from "./types";

const TONE: Record<CampaignStatus, "neutral" | "accent" | "good" | "warn" | "bad"> = {
  draft: "neutral",
  approved: "accent",
  sending: "good",
  paused: "warn",
  done: "neutral",
  cancelled: "bad",
};
const LABEL: Record<CampaignStatus, string> = {
  draft: "Draft",
  approved: "Approved",
  sending: "Sending",
  paused: "Paused",
  done: "Done",
  cancelled: "Cancelled",
};

export function CampaignBadge({ status }: { status: CampaignStatus }) {
  return <Badge tone={TONE[status]}>{LABEL[status]}</Badge>;
}

export function TemplateBadge({ status }: { status: string | null }) {
  if (!status) return <Badge>Not submitted</Badge>;
  const tone = status === "APPROVED" ? "good" : status === "PENDING" ? "warn" : "bad";
  return <Badge tone={tone}>{status.charAt(0) + status.slice(1).toLowerCase()}</Badge>;
}

/** Why a campaign stopped or a recipient was skipped, in words. */
export const REASON: Record<string, string> = {
  budget: "Its budget is used up",
  monthly_cap: "The monthly message cap is reached",
  quality_yellow: "WhatsApp quality rating is Yellow — all marketing paused",
  quality_red: "WhatsApp quality rating is Red — marketing stopped",
  template_not_approved: "The template is no longer approved by Meta",
  template_error: "Meta rejected the template's values",
  meta_auth: "The WhatsApp connection needs attention",
  meta_errors: "Too many failed sends in a row",
  meta_spam_limit: "Meta is limiting sends — stopped to protect the number",
  no_channel: "No WhatsApp number connected",
  no_template: "No template",
  unpriced: "No price configured for a recipient's country",
  paused_by_team: "Paused by the team",
  opted_out: "Opted out since the campaign was built",
  not_opted_in: "Not opted in",
  frequency_cap: "Already had a marketing message this week",
  no_answer_from_meta: "No answer from WhatsApp (not re-sent)",
};

export const PROBLEM: Record<string, string> = {
  template_not_approved: "The template is not approved by Meta yet.",
  no_template: "Pick a template.",
  bindings_mismatch: "Fill in every variable of the template.",
  invalid_bindings: "A variable is missing its value or fallback.",
  no_segment: "Choose who receives it.",
  no_recipients: "Nobody matches this audience.",
  template_category: "The template needs a Marketing or Utility category.",
};

export function reason(key: string | null | undefined): string {
  if (!key) return "";
  return REASON[key] ?? key.replace(/_/g, " ");
}

/** "{{1}}" placeholders in a body, in order of first use. */
export function placeholders(body: string): number[] {
  const out: number[] = [];
  for (const m of body.matchAll(/\{\{\s*(\d+)\s*\}\}/g)) {
    const n = Number(m[1]);
    if (!out.includes(n)) out.push(n);
  }
  return out;
}
