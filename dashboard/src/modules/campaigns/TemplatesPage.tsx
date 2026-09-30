import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Plus, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { dateTime } from "@/lib/format";
import { placeholders, TemplateBadge } from "./shared";
import type { Check, Draft, Template, Variable } from "./types";

export default function TemplatesPage() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const [editing, setEditing] = useState<Template | "new" | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [fromDraft, setFromDraft] = useState<Draft | null>(null);
  const list = useQuery({ queryKey: ["campaigns", "templates"], queryFn: () => api<Template[]>("/m/campaigns/templates") });
  const sync = useMutation({
    mutationFn: () => api<{ updated: number; imported: number }>("/m/campaigns/templates/sync", { method: "POST" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["campaigns", "templates"] }),
  });
  const submit = useMutation({
    mutationFn: (id: string) => api<Template>(`/m/campaigns/templates/${id}/submit`, { method: "POST" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["campaigns", "templates"] }),
  });

  return (
    <div>
      <Link to="/campaigns" className="mb-2 inline-flex items-center gap-1 text-sm text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" /> Campaigns
      </Link>
      <PageTitle title="Message templates">
        {isAdmin ? (
          <>
            <Button variant="secondary" disabled={sync.isPending} onClick={() => sync.mutate()}>
              <RefreshCw /> Sync from WhatsApp
            </Button>
            <Button variant="secondary" onClick={() => setDrafting(true)}>
              <Sparkles /> Draft with AI
            </Button>
            <Button onClick={() => setEditing("new")}>
              <Plus /> New
            </Button>
          </>
        ) : null}
      </PageTitle>
      <p className="mb-4 text-sm text-muted">
        Marketing messages must be templates Meta has approved. Write one here, submit it, and it can be used in a campaign once approved
        (usually minutes to a day).
      </p>
      {sync.data ? (
        <p className="mb-3 text-sm text-ink-2">
          Synced: {sync.data.updated} updated, {sync.data.imported} imported.
        </p>
      ) : null}
      <ErrorNote error={list.error ?? sync.error ?? submit.error} />
      <Card className="overflow-hidden">
        {list.isPending ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : !list.data?.length ? (
          <Empty title="No templates yet">Draft one with AI, or write your own.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {list.data.map((t) => (
              <li key={t.id} className="space-y-2 px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-medium">
                    {t.name}{" "}
                    <span className="text-xs font-normal text-muted">
                      · {t.language} · {(t.category ?? "").toLowerCase()}
                    </span>
                  </p>
                  <TemplateBadge status={t.meta_status} />
                </div>
                <p className="whitespace-pre-wrap text-sm text-ink-2" dir="auto">
                  {t.body}
                </p>
                {t.rejected_reason ? <p className="text-sm text-bad">Meta: {t.rejected_reason.replace(/_/g, " ").toLowerCase()}</p> : null}
                {t.check.errors.length && (t.meta_status === null || t.meta_status === "REJECTED") ? (
                  <p className="text-sm text-warn">{t.check.errors[0]}</p>
                ) : null}
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                  {t.source === "meta" ? <span>Made in Meta's tools</span> : null}
                  {t.submitted_at ? <span>Submitted {dateTime(t.submitted_at)}</span> : null}
                  {isAdmin && (t.meta_status === null || t.meta_status === "REJECTED") ? (
                    <>
                      <Button size="sm" variant="ghost" onClick={() => setEditing(t)}>
                        Edit
                      </Button>
                      <Button size="sm" disabled={submit.isPending || t.check.errors.length > 0} onClick={() => submit.mutate(t.id)}>
                        Submit to Meta
                      </Button>
                    </>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {drafting ? (
        <DraftSheet
          onClose={() => setDrafting(false)}
          onUse={(d) => {
            setDrafting(false);
            setFromDraft(d);
            setEditing("new");
          }}
        />
      ) : null}
      {editing ? (
        <TemplateSheet
          template={editing === "new" ? null : editing}
          draft={editing === "new" ? fromDraft : null}
          onClose={() => {
            setEditing(null);
            setFromDraft(null);
          }}
          onSaved={() => {
            setEditing(null);
            setFromDraft(null);
            void client.invalidateQueries({ queryKey: ["campaigns", "templates"] });
          }}
        />
      ) : null}
    </div>
  );
}

function DraftSheet({ onClose, onUse }: { onClose: () => void; onUse: (d: Draft) => void }) {
  const [brief, setBrief] = useState({ objective: "", audience: "", offer: "", products: "", language: "en" });
  const draft = useMutation({ mutationFn: () => api<Draft>("/m/campaigns/templates/draft", { method: "POST", body: brief }) });
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title="Draft with AI"
      description="A suggestion for you to edit — nothing is saved or sent."
      footer={
        draft.data ? (
          <div className="flex gap-2">
            <Button variant="secondary" className="flex-1" disabled={draft.isPending} onClick={() => draft.mutate()}>
              Try again
            </Button>
            <Button className="flex-1" onClick={() => onUse(draft.data)}>
              Use this draft
            </Button>
          </div>
        ) : (
          <Button className="w-full" disabled={brief.objective.trim().length < 3 || draft.isPending} onClick={() => draft.mutate()}>
            {draft.isPending ? "Writing…" : "Write a draft"}
          </Button>
        )
      }
    >
      <div className="space-y-4">
        <Field label="What should it achieve?">
          <Input
            value={brief.objective}
            onChange={(e) => setBrief({ ...brief, objective: e.target.value })}
            placeholder="e.g. win back customers who stopped ordering"
          />
        </Field>
        <Field label="Who is it for?">
          <Input
            value={brief.audience}
            onChange={(e) => setBrief({ ...brief, audience: e.target.value })}
            placeholder="e.g. no order in 60 days"
          />
        </Field>
        <Field label="The offer or news">
          <Input
            value={brief.offer}
            onChange={(e) => setBrief({ ...brief, offer: e.target.value })}
            placeholder="e.g. free delivery this week"
          />
        </Field>
        <Field label="Products to feature">
          <Input value={brief.products} onChange={(e) => setBrief({ ...brief, products: e.target.value })} />
        </Field>
        <Field label="Language">
          <Select value={brief.language} onChange={(e) => setBrief({ ...brief, language: e.target.value })}>
            <option value="en">English</option>
            <option value="ar">Arabic</option>
          </Select>
        </Field>
        <ErrorNote error={draft.error} />
        {draft.data ? (
          <figure className="space-y-2 rounded-xl border border-line p-3">
            <p className="whitespace-pre-wrap text-sm" dir="auto">
              {draft.data.body}
            </p>
            <figcaption className="text-xs text-muted">{draft.data.rationale}</figcaption>
            <Problems check={draft.data.check} />
          </figure>
        ) : null}
      </div>
    </Sheet>
  );
}

function Problems({ check }: { check: Check }) {
  if (!check.errors.length && !check.warnings.length) return <p className="text-xs text-good">Meets Meta's template rules.</p>;
  return (
    <ul className="space-y-0.5 text-xs">
      {check.errors.map((e) => (
        <li key={e} className="text-bad">
          {e}
        </li>
      ))}
      {check.warnings.map((w) => (
        <li key={w} className="text-warn">
          {w}
        </li>
      ))}
    </ul>
  );
}

function TemplateSheet({
  template,
  draft,
  onClose,
  onSaved,
}: {
  template: Template | null;
  draft: Draft | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(template?.name ?? draft?.name ?? "");
  const [language, setLanguage] = useState(template?.language ?? draft?.language ?? "en");
  const [category, setCategory] = useState(template?.category ?? draft?.category ?? "MARKETING");
  const [body, setBody] = useState(template?.body ?? draft?.body ?? "");
  const [variables, setVariables] = useState<Variable[]>(template?.variables ?? draft?.variables ?? []);
  const slots = placeholders(body);

  useEffect(() => {
    setVariables((vs) => slots.map((n) => vs.find((v) => v.index === n) ?? { index: n, meaning: "", example: "" }));
  }, [slots.join(",")]); // re-run only when the set of placeholders changes

  const live = useQuery({
    queryKey: ["campaigns", "check", body, variables],
    queryFn: () =>
      api<Check>("/m/campaigns/templates/check", { method: "POST", body: { body, variables: variables.filter((v) => v.example.trim()) } }),
    enabled: body.trim().length > 0,
  });
  const save = useMutation({
    mutationFn: () => {
      const vars = variables.map((v) => ({ ...v, example: v.example.trim() }));
      return template
        ? api<Template>(`/m/campaigns/templates/${template.id}`, { method: "PATCH", body: { body, category, variables: vars } })
        : api<Template>("/m/campaigns/templates", {
            method: "POST",
            body: { name: name.trim(), language, category, body, variables: vars },
          });
    },
    onSuccess: onSaved,
  });

  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title={template ? `Edit ${template.name}` : "New template"}
      description="Saved here first; submit it to Meta from the list."
      footer={
        <Button
          className="w-full"
          disabled={save.isPending || !name.trim() || !body.trim() || !!live.data?.errors.length}
          onClick={() => save.mutate()}
        >
          Save
        </Button>
      }
    >
      <div className="space-y-4">
        {!template ? (
          <Field label="Name" hint="Lower case letters, numbers and _ — Meta's rule">
            <Input value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))} />
          </Field>
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          {!template ? (
            <Field label="Language">
              <Select value={language} onChange={(e) => setLanguage(e.target.value)}>
                <option value="en">English</option>
                <option value="ar">Arabic</option>
              </Select>
            </Field>
          ) : null}
          <Field label="Category" hint="Offers are Marketing">
            <Select value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="MARKETING">Marketing</option>
              <option value="UTILITY">Utility</option>
            </Select>
          </Field>
        </div>
        <Field label="Message" hint="Use {{1}}, {{2}} for what changes per customer, e.g. their name">
          <Textarea rows={6} dir="auto" value={body} onChange={(e) => setBody(e.target.value)} />
        </Field>
        <p className="text-right text-xs text-muted">{body.trim().length} / 700</p>
        {variables.map((v, i) => (
          <div key={v.index} className="grid grid-cols-[3.5rem_1fr_1fr] items-end gap-2">
            <span className="pb-2.5 text-sm font-medium">{`{{${v.index}}}`}</span>
            <Field label="Means">
              <Input
                value={v.meaning}
                onChange={(e) => setVariables(variables.map((x, j) => (j === i ? { ...x, meaning: e.target.value } : x)))}
                placeholder="first name"
              />
            </Field>
            <Field label="Example for Meta">
              <Input
                value={v.example}
                onChange={(e) => setVariables(variables.map((x, j) => (j === i ? { ...x, example: e.target.value } : x)))}
                placeholder="Ahmed"
              />
            </Field>
          </div>
        ))}
        {live.data ? <Problems check={live.data} /> : null}
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}
