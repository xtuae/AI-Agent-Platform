import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, Plus, QrCode } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import type { OptinLink } from "@/lib/types";

interface LinksResponse {
  links: OptinLink[];
  whatsapp_ready: boolean;
}

const SOURCES: [string, string][] = [
  ["qr_van", "Delivery van"],
  ["qr_store", "Shop or counter"],
  ["qr_flyer", "Flyer or leaflet"],
  ["qr_invoice", "Invoice or receipt"],
  ["website", "Website"],
  ["qr_code", "Other QR code"],
];

function sourceLabel(key: string): string {
  return SOURCES.find(([k]) => k === key)?.[1] ?? key.replace(/_/g, " ");
}

/** Consent pages behind QR codes: the customer reads the exact wording, taps through to WhatsApp and
 *  sends a prefilled message. That message is the proof of consent. */
export function OptinLinks() {
  const isAdmin = useCan("admin");
  const q = useQuery({ queryKey: ["optin-links"], queryFn: () => api<LinksResponse>("/optin/links") });
  const [editing, setEditing] = useState<OptinLink | "new" | null>(null);
  const [showing, setShowing] = useState<OptinLink | null>(null);

  return (
    <Card>
      <CardHeader
        title="Opt-in QR codes"
        subtitle="Customers scan, read what they are agreeing to, and confirm on WhatsApp. Each opt-in keeps that exact wording as proof."
        action={
          isAdmin ? (
            <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
              <Plus /> New
            </Button>
          ) : null
        }
      />
      {q.isPending ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : q.isError ? (
        <div className="p-4">
          <ErrorNote error={q.error} />
        </div>
      ) : (
        <>
          {!q.data.whatsapp_ready ? (
            <p className="mx-4 mt-3 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
              No WhatsApp number is connected yet, so the pages cannot open WhatsApp. They will work once it is.
            </p>
          ) : null}
          {q.data.links.length === 0 ? (
            <Empty title="No QR codes yet">{isAdmin ? "Create one for your van, shop counter or flyers." : null}</Empty>
          ) : (
            <ul className="mt-2 divide-y divide-line border-t border-line">
              {q.data.links.map((l) => (
                <li key={l.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {l.label} {!l.is_active ? <Badge className="ml-1">Switched off</Badge> : null}
                    </p>
                    <p className="truncate text-xs text-muted">
                      {sourceLabel(l.source)} · {l.visits} tap{l.visits === 1 ? "" : "s"} · {l.opted_in} opted in
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button size="sm" variant="secondary" onClick={() => setShowing(l)}>
                      <QrCode /> QR
                    </Button>
                    {isAdmin ? (
                      <Button size="sm" variant="ghost" onClick={() => setEditing(l)}>
                        Edit
                      </Button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {editing ? <LinkEditor link={editing === "new" ? null : editing} onClose={() => setEditing(null)} /> : null}
      {showing ? <QrSheet link={showing} onClose={() => setShowing(null)} /> : null}
    </Card>
  );
}

function QrSheet({ link, onClose }: { link: OptinLink; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["optin-qr", link.id],
    queryFn: () => api<{ url: string; svg: string }>(`/optin/links/${link.id}/qr`),
  });
  const [copied, setCopied] = useState(false);
  const download = () => {
    if (!q.data) return;
    const url = URL.createObjectURL(new Blob([q.data.svg], { type: "image/svg+xml" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `qr-${link.label.replace(/[^a-z0-9]+/gi, "-").toLowerCase() || link.code}.svg`;
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <Sheet open onOpenChange={(v) => !v && onClose()} title={link.label} description="Print this where customers will see it.">
      {q.isPending ? (
        <Spinner />
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : (
        <div className="space-y-4">
          {/* SVG generated by our own server from the page URL: no user content inside */}
          <div
            className="mx-auto w-full max-w-64 rounded-lg bg-white p-2 [&>svg]:h-auto [&>svg]:w-full"
            dangerouslySetInnerHTML={{ __html: q.data.svg }}
          />
          <p className="break-all text-center text-sm text-ink-2">{q.data.url}</p>
          <div className="grid grid-cols-2 gap-2">
            <Button variant="secondary" onClick={download}>
              <Download /> Download
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                void navigator.clipboard.writeText(q.data.url).then(() => setCopied(true));
              }}
            >
              <Copy /> {copied ? "Copied" : "Copy link"}
            </Button>
          </div>
          <div className="rounded-lg border border-line p-3 text-sm">
            <p className="font-medium">{link.heading}</p>
            <p className="mt-1 whitespace-pre-line text-ink-2">{link.wording}</p>
          </div>
        </div>
      )}
    </Sheet>
  );
}

function LinkEditor({ link, onClose }: { link: OptinLink | null; onClose: () => void }) {
  const client = useQueryClient();
  const [f, setF] = useState({
    label: link?.label ?? "",
    source: link?.source ?? "qr_van",
    language: link?.language ?? ("en" as "en" | "ar"),
    heading: link?.heading ?? "",
    wording: link?.wording ?? "",
    prefill: link?.prefill ?? "",
    is_active: link?.is_active ?? true,
  });
  const defaults = useQuery({
    queryKey: ["optin-defaults", f.language],
    queryFn: () => api<{ heading: string; wording: string; prefill: string }>("/optin/defaults", { query: { language: f.language } }),
    enabled: link === null,
  });
  // a new link starts from suggested wording in the chosen language
  useEffect(() => {
    if (link === null && defaults.data) setF((cur) => ({ ...cur, ...defaults.data }));
  }, [link, defaults.data]);

  const save = useMutation({
    mutationFn: () => {
      const { language, ...rest } = f;
      return link
        ? api<OptinLink>(`/optin/links/${link.id}`, { method: "PATCH", body: rest })
        : api<OptinLink>("/optin/links", { method: "POST", body: { ...rest, language, is_active: undefined } });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["optin-links"] });
      onClose();
    },
  });
  const ok = f.label && f.heading.length >= 3 && f.wording.length >= 20 && f.prefill.length >= 2;
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      title={link ? "Edit QR code" : "New QR code"}
      description={link ? "Changes apply to future scans. Earlier opt-ins keep the wording they saw." : undefined}
      footer={
        <Button className="w-full" disabled={!ok || save.isPending} onClick={() => save.mutate()}>
          {link ? "Save" : "Create"}
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Name" hint="For your team only, e.g. “Van 3 sticker”">
          <Input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} />
        </Field>
        <Field label="Where it will be">
          <Select value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })}>
            {SOURCES.map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
        {link === null ? (
          <Field label="Page language">
            <Select value={f.language} onChange={(e) => setF({ ...f, language: e.target.value as "en" | "ar" })}>
              <option value="en">English</option>
              <option value="ar">Arabic</option>
            </Select>
          </Field>
        ) : null}
        <Field label="Heading">
          <Input dir="auto" value={f.heading} onChange={(e) => setF({ ...f, heading: e.target.value })} />
        </Field>
        <Field label="What the customer agrees to" hint="Shown word for word and kept as proof with every opt-in.">
          <Textarea dir="auto" rows={4} value={f.wording} onChange={(e) => setF({ ...f, wording: e.target.value })} />
        </Field>
        <Field label="Message WhatsApp fills in" hint="A short reference code is added so we can match the opt-in to this page.">
          <Input dir="auto" value={f.prefill} onChange={(e) => setF({ ...f, prefill: e.target.value })} />
        </Field>
        {link ? (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} />
            Page is on (switch off to retire a printed code)
          </label>
        ) : null}
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}
