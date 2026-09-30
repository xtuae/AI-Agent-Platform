import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Plus, ShieldAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { aed, dateTime } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import { cn } from "@/lib/utils";
import { CampaignBadge, placeholders, PROBLEM, reason } from "./shared";
import type { Binding, Campaign, CampaignDetail, Guardrails, Preview, SegmentField, Template } from "./types";

export default function CampaignsPage() {
  const isAdmin = useCan("admin");
  const [openId, setOpenId] = useState<string | null>(null);
  const [building, setBuilding] = useState<Campaign | "new" | null>(null);
  const list = useQuery({ queryKey: ["campaigns"], queryFn: () => api<Campaign[]>("/m/campaigns"), refetchInterval: usePollInterval() });
  const guard = useQuery({ queryKey: ["campaigns", "guardrails"], queryFn: () => api<Guardrails>("/m/campaigns/guardrails") });

  return (
    <div>
      <PageTitle title="Campaigns">
        <Button variant="secondary" asChild>
          <Link to="/campaigns/templates">
            <FileText /> Templates
          </Link>
        </Button>
        {isAdmin ? (
          <Button onClick={() => setBuilding("new")}>
            <Plus /> New campaign
          </Button>
        ) : null}
      </PageTitle>

      {guard.data ? <GuardBanner g={guard.data} /> : null}

      <ErrorNote error={list.error} />
      <Card className="overflow-hidden">
        {list.isPending ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : !list.data?.length ? (
          <Empty title="No campaigns yet">
            A campaign sends an approved WhatsApp template to opted-in customers. Start with a template.
          </Empty>
        ) : (
          <ul className="divide-y divide-line">
            {list.data.map((c) => (
              <li key={c.id}>
                <button
                  className="grid w-full grid-cols-[1fr_auto] gap-x-3 px-4 py-3 text-left hover:bg-line/30 md:grid-cols-[1.5fr_1fr_auto]"
                  onClick={() => setOpenId(c.id)}
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium">{c.name}</p>
                    <p className="truncate text-xs text-muted">
                      {c.template_name ?? "No template"}
                      {c.status === "paused" && c.paused_reason ? ` · ${reason(c.paused_reason)}` : ""}
                    </p>
                  </div>
                  <div className="text-right md:order-last">
                    <CampaignBadge status={c.status} />
                  </div>
                  <p className="tabular col-span-2 mt-1 text-xs text-ink-2 md:col-span-1 md:mt-0 md:self-center">
                    {c.recipient_count
                      ? `${c.sent_count}/${c.recipient_count} sent · ${c.read_count} read · ${c.reply_count} ${c.reply_count === 1 ? "reply" : "replies"}`
                      : "Not started"}
                    {Number(c.spend_aed) > 0 ? ` · ${aed(c.spend_aed)}` : ""}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <CampaignDrawer id={openId} onClose={() => setOpenId(null)} onEdit={(c) => setBuilding(c)} />
      {building ? (
        <Builder
          campaign={building === "new" ? null : building}
          onClose={() => setBuilding(null)}
          onSaved={(c) => {
            setBuilding(null);
            setOpenId(c.id);
          }}
        />
      ) : null}
    </div>
  );
}

function GuardBanner({ g }: { g: Guardrails }) {
  if (g.quality_block) {
    return (
      <p role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/5 px-3 py-2 text-sm text-bad">
        <ShieldAlert className="mt-0.5 size-4 shrink-0" /> {reason(g.quality_block)}. Nothing is sent until Meta rates the number Green
        again.
      </p>
    );
  }
  return (
    <p className="mb-4 text-xs text-muted">
      WhatsApp quality: <span className="font-medium text-ink-2">{g.quality_rating ?? "unknown"}</span>
      {g.tier_limit ? ` · up to ${g.tier_limit.toLocaleString("en")} customers a day (${g.sent_last_24h} used)` : ""} · at most one
      marketing message per customer every {g.frequency_days} days ·{" "}
      {g.may_send_now ? "sending allowed now" : `next sending window ${dateTime(g.next_send_time)}`}
    </p>
  );
}

// ---------------------------------------------------------------- one campaign

function CampaignDrawer({ id, onClose, onEdit }: { id: string | null; onClose: () => void; onEdit: (c: Campaign) => void }) {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const q = useQuery({
    queryKey: ["campaign", id],
    queryFn: () => api<CampaignDetail>(`/m/campaigns/${id}`),
    enabled: id !== null,
    refetchInterval: usePollInterval(),
  });
  const preview = useQuery({
    queryKey: ["campaign", id, "preview"],
    queryFn: () => api<Preview>(`/m/campaigns/${id}/preview`),
    enabled: id !== null && (q.data?.status === "draft" || q.data?.status === "approved"),
  });
  const act = useMutation({
    mutationFn: (action: string) => api<Campaign>(`/m/campaigns/${id}/${action}`, { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["campaigns"] });
      void client.invalidateQueries({ queryKey: ["campaign", id] });
    },
  });
  const c = q.data;
  const orders = c?.results.modules.orders as { orders: number; orders_value_aed: string; window_days: number } | undefined;

  return (
    <Sheet
      open={id !== null}
      onOpenChange={(v) => {
        if (!v) {
          act.reset();
          onClose();
        }
      }}
      wide
      title={c?.name ?? "Campaign"}
      description={c ? `${c.template_name ?? "No template"} · created ${dateTime(c.created_at)}` : undefined}
    >
      {!c ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-6">
          <div className="flex flex-wrap items-center gap-2">
            <CampaignBadge status={c.status} />
            {c.paused_reason ? <span className="text-sm text-warn">{reason(c.paused_reason)}</span> : null}
          </div>
          <ErrorNote error={act.error} />

          {c.status === "draft" || c.status === "approved" ? (
            <section className="space-y-2">
              <h3 className="text-xs font-medium uppercase tracking-wide text-muted">Before anything sends</h3>
              {preview.isPending ? (
                <Spinner />
              ) : preview.data ? (
                <>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                    <dt className="text-muted">Recipients</dt>
                    <dd className="tabular">{preview.data.recipients.toLocaleString("en")} opted-in customers</dd>
                    <dt className="text-muted">Estimated cost</dt>
                    <dd className="tabular">
                      {preview.data.estimated_cost_aed ? aed(preview.data.estimated_cost_aed) : "Unknown (no rate for a country)"}
                    </dd>
                    {c.budget_cap_aed ? (
                      <>
                        <dt className="text-muted">Budget cap</dt>
                        <dd className="tabular">{aed(c.budget_cap_aed)} — it pauses there</dd>
                      </>
                    ) : null}
                  </dl>
                  {preview.data.sample ? (
                    <figure className="rounded-xl border border-line bg-good/5 p-3 text-sm">
                      <figcaption className="mb-1 text-xs text-muted">What {preview.data.sample_to} would receive</figcaption>
                      <p className="whitespace-pre-wrap" dir="auto">
                        {preview.data.sample}
                      </p>
                    </figure>
                  ) : null}
                  {preview.data.problems.length ? (
                    <ul className="list-disc space-y-0.5 pl-5 text-sm text-warn">
                      {preview.data.problems.map((p) => (
                        <li key={p}>{PROBLEM[p] ?? p}</li>
                      ))}
                    </ul>
                  ) : null}
                </>
              ) : null}
            </section>
          ) : (
            <section>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Results</h3>
              <div className="grid grid-cols-3 gap-2 text-center">
                {[
                  ["Sent", c.sent_count],
                  ["Delivered", c.delivered_count],
                  ["Read", c.read_count],
                  ["Replies", c.reply_count],
                  ["Skipped", c.skipped_count],
                  ["Failed", c.failed_count],
                ].map(([label, n]) => (
                  <div key={label} className="rounded-lg border border-line px-2 py-2">
                    <p className="tabular text-lg font-semibold">{Number(n).toLocaleString("en")}</p>
                    <p className="text-xs text-muted">{label}</p>
                  </div>
                ))}
              </div>
              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted">Spend</dt>
                <dd className="tabular">
                  {aed(c.spend_aed)}
                  {c.budget_cap_aed ? ` of ${aed(c.budget_cap_aed)}` : ""}
                </dd>
                {orders ? (
                  <>
                    <dt className="text-muted">Orders after</dt>
                    <dd className="tabular">
                      {orders.orders} ({aed(orders.orders_value_aed)}) within {orders.window_days} days
                    </dd>
                  </>
                ) : null}
                {c.started_at ? (
                  <>
                    <dt className="text-muted">Started</dt>
                    <dd>{dateTime(c.started_at)}</dd>
                  </>
                ) : null}
              </dl>
              {Object.keys(c.results.skip_reasons).length ? (
                <ul className="mt-3 space-y-0.5 text-xs text-muted">
                  {Object.entries(c.results.skip_reasons).map(([k, n]) => (
                    <li key={k}>
                      {n} · {reason(k)}
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          )}

          {isAdmin ? (
            <div className="flex flex-wrap gap-2">
              {c.status === "draft" ? (
                <>
                  <Button variant="secondary" onClick={() => onEdit(c)}>
                    Edit
                  </Button>
                  <Button
                    disabled={act.isPending || !!preview.data?.problems.length}
                    onClick={() => {
                      if (window.confirm(`Approve "${c.name}" for ${preview.data?.recipients ?? 0} customers? It can then be started.`))
                        act.mutate("approve");
                    }}
                  >
                    Approve
                  </Button>
                </>
              ) : null}
              {c.status === "approved" ? (
                <Button
                  disabled={act.isPending}
                  onClick={() => {
                    if (window.confirm("Start sending now? Messages go out within business hours, a batch a minute.")) act.mutate("start");
                  }}
                >
                  Start sending
                </Button>
              ) : null}
              {c.status === "sending" ? (
                <Button variant="secondary" disabled={act.isPending} onClick={() => act.mutate("pause")}>
                  Pause
                </Button>
              ) : null}
              {c.status === "paused" ? (
                <Button disabled={act.isPending} onClick={() => act.mutate("resume")}>
                  Resume
                </Button>
              ) : null}
              {c.status !== "done" && c.status !== "cancelled" ? (
                <Button
                  variant="danger"
                  disabled={act.isPending}
                  onClick={() => {
                    if (window.confirm("Cancel this campaign? Nothing more is sent.")) act.mutate("cancel");
                  }}
                >
                  Cancel campaign
                </Button>
              ) : null}
            </div>
          ) : null}
          {c.approved_at ? <p className="text-xs text-muted">Approved {dateTime(c.approved_at)}</p> : null}
        </div>
      )}
    </Sheet>
  );
}

// ---------------------------------------------------------------- builder

type Rule = { name: string; value: string };

function toRules(segment: Record<string, unknown> | null): Rule[] {
  return Object.entries(segment ?? {})
    .filter(([k]) => k !== "opt_in_status")
    .map(([name, v]) => ({ name, value: Array.isArray(v) ? v.join(", ") : String(v) }));
}

function toDefinition(rules: Rule[], fields: SegmentField[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const r of rules) {
    const f = fields.find((x) => x.name === r.name);
    if (!f || !r.value.trim()) continue;
    out[r.name] =
      f.kind === "int"
        ? Number(r.value)
        : f.kind === "text_list"
          ? r.value
              .split(",")
              .map((x) => x.trim())
              .filter(Boolean)
          : r.value.trim();
  }
  return out;
}

function Builder({ campaign, onClose, onSaved }: { campaign: Campaign | null; onClose: () => void; onSaved: (c: Campaign) => void }) {
  const client = useQueryClient();
  const templates = useQuery({ queryKey: ["campaigns", "templates"], queryFn: () => api<Template[]>("/m/campaigns/templates") });
  const meta = useQuery({
    queryKey: ["campaigns", "segment-fields"],
    queryFn: () =>
      api<{ fields: SegmentField[]; variable_sources: { source: Binding["source"]; label: string }[] }>("/m/campaigns/segment-fields"),
  });
  const [name, setName] = useState(campaign?.name ?? "");
  const [templateId, setTemplateId] = useState(campaign?.template_id ?? "");
  const [bindings, setBindings] = useState<Binding[]>(campaign?.variable_bindings ?? []);
  const [rules, setRules] = useState<Rule[]>(toRules(campaign?.segment ?? null));
  const [budget, setBudget] = useState(campaign?.budget_cap_aed ?? "");
  const approved = (templates.data ?? []).filter((t) => t.meta_status === "APPROVED");
  const template = approved.find((t) => t.id === templateId);
  const slots = useMemo(() => placeholders(template?.body ?? ""), [template]);
  const fields = meta.data?.fields ?? [];
  const definition = toDefinition(rules, fields);

  // one binding per placeholder, defaulting to the contact's first name (only once the template
  // is known: while templates load, the saved bindings must not be trimmed away)
  useEffect(() => {
    if (!template) return;
    setBindings((b) =>
      slots.map((_, i) => b[i] ?? (i === 0 ? { source: "contact.first_name", fallback: "there" } : { source: "text", value: "" })),
    );
  }, [slots, template]);

  const count = useQuery({
    queryKey: ["campaigns", "count", definition],
    queryFn: () => api<{ recipients: number }>("/m/campaigns/segments/count", { method: "POST", body: { definition } }),
    enabled: meta.isSuccess,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        template_id: templateId || null,
        segment: definition,
        variable_bindings: bindings,
        budget_cap_aed: budget.trim() || null,
      };
      return campaign
        ? api<Campaign>(`/m/campaigns/${campaign.id}`, { method: "PATCH", body })
        : api<Campaign>("/m/campaigns", { method: "POST", body });
    },
    onSuccess: (c) => {
      void client.invalidateQueries({ queryKey: ["campaigns"] });
      void client.invalidateQueries({ queryKey: ["campaign", c.id] });
      onSaved(c);
    },
  });

  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title={campaign ? `Edit ${campaign.name}` : "New campaign"}
      description="Saved as a draft. Nothing sends until an admin approves and starts it."
      footer={
        <Button className="w-full" disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
          Save draft
        </Button>
      }
    >
      <div className="space-y-5">
        <Field label="Name" hint="Only your team sees this">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Snack offer, October" />
        </Field>

        <section className="space-y-2">
          <Field label="Template" hint="Only templates Meta has approved can be sent">
            <Select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
              <option value="">Choose a template…</option>
              {approved.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.language})
                </option>
              ))}
            </Select>
          </Field>
          {!approved.length && templates.isSuccess ? (
            <p className="text-sm text-muted">
              No approved templates yet.{" "}
              <Link className="text-accent-ink" to="/campaigns/templates">
                Write one
              </Link>
              .
            </p>
          ) : null}
          {template?.body ? (
            <p className="whitespace-pre-wrap rounded-lg bg-line/40 p-3 text-sm" dir="auto">
              {template.body}
            </p>
          ) : null}
          {slots.map((n, i) => {
            const b = bindings[i] ?? { source: "text" as const, value: "" };
            const v = template?.variables.find((x) => x.index === n);
            return (
              <div key={n} className="grid grid-cols-1 gap-2 rounded-lg border border-line p-2 sm:grid-cols-[5rem_1fr_1fr] sm:items-end">
                <span className="text-sm font-medium sm:pb-2.5">
                  {`{{${n}}}`}
                  {v?.meaning ? <span className="block text-xs font-normal text-muted">{v.meaning}</span> : null}
                </span>
                <Field label="Filled with">
                  <Select
                    value={b.source}
                    onChange={(e) =>
                      setBindings(
                        bindings.map((x, j) => (j === i ? { source: e.target.value as Binding["source"], value: "", fallback: "" } : x)),
                      )
                    }
                  >
                    {(meta.data?.variable_sources ?? []).map((s) => (
                      <option key={s.source} value={s.source}>
                        {s.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                {b.source === "text" ? (
                  <Field label="Text">
                    <Input
                      value={b.value ?? ""}
                      onChange={(e) => setBindings(bindings.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                    />
                  </Field>
                ) : (
                  <Field label="If unknown">
                    <Input
                      value={b.fallback ?? ""}
                      onChange={(e) => setBindings(bindings.map((x, j) => (j === i ? { ...x, fallback: e.target.value } : x)))}
                    />
                  </Field>
                )}
              </div>
            );
          })}
        </section>

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-ink-2">Who receives it</h3>
            <span className={cn("tabular text-sm", count.isFetching && "opacity-60")}>
              {count.data ? `${count.data.recipients.toLocaleString("en")} customers` : null}
            </span>
          </div>
          <p className="text-xs text-muted">Only customers who opted in to offers — always. Add filters to narrow it.</p>
          <ErrorNote error={count.error} />
          {rules.map((r, i) => {
            const f = fields.find((x) => x.name === r.name);
            return (
              <div key={i} className="flex items-end gap-2">
                <Field label={f?.label ?? r.name} hint={f?.help} className="flex-1">
                  {f?.choices.length && f.kind === "text" ? (
                    <Select
                      value={r.value}
                      onChange={(e) => setRules(rules.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                    >
                      <option value="">Choose…</option>
                      {f.choices.map((c) => (
                        <option key={c}>{c}</option>
                      ))}
                    </Select>
                  ) : (
                    <Input
                      inputMode={f?.kind === "int" ? "numeric" : undefined}
                      placeholder={f?.kind === "text_list" ? "Comma separated" : undefined}
                      value={r.value}
                      onChange={(e) =>
                        setRules(
                          rules.map((x, j) =>
                            j === i ? { ...x, value: f?.kind === "int" ? e.target.value.replace(/\D/g, "") : e.target.value } : x,
                          ),
                        )
                      }
                    />
                  )}
                </Field>
                <Button variant="ghost" size="icon" aria-label="Remove filter" onClick={() => setRules(rules.filter((_, j) => j !== i))}>
                  <X />
                </Button>
              </div>
            );
          })}
          <Select
            aria-label="Add a filter"
            value=""
            onChange={(e) => e.target.value && setRules([...rules, { name: e.target.value, value: "" }])}
          >
            <option value="">+ Add a filter…</option>
            {fields
              .filter((f) => !rules.some((r) => r.name === f.name))
              .map((f) => (
                <option key={f.name} value={f.name}>
                  {f.label}
                </option>
              ))}
          </Select>
        </section>

        <Field label="Budget cap (AED)" hint="Optional. Sending pauses when the campaign's WhatsApp charges reach it.">
          <Input inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value.replace(/[^\d.]/g, ""))} />
        </Field>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}
