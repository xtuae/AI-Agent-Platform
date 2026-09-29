import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useAuth, useCan } from "@/lib/auth";
import { dateTime, label, phone, STATUS_LABEL } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { CustomerDetail, CustomerRow, OptIn, Page } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useModules } from "@/modules";

const OPT_TONE: Record<OptIn, "neutral" | "good" | "bad"> = { pending: "neutral", opted_in: "good", opted_out: "bad" };
const PAGE = 50;

export default function ContactsPage() {
  const contactLabel = useAuth().session?.tenant.contact_label ?? "Customers";
  const [q, setQ] = useState("");
  const [optIn, setOptIn] = useState<OptIn | "">("");
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const query = { q: q.trim() || undefined, opt_in: optIn || undefined, limit: PAGE, offset: page * PAGE };
  const list = useQuery({
    queryKey: ["customers", query],
    queryFn: () => api<Page<CustomerRow>>("/contacts", { query }),
    placeholderData: keepPreviousData,
    refetchInterval: usePollInterval(),
  });

  return (
    <div>
      <PageTitle title={contactLabel}>
        {list.data ? <span className="text-sm text-muted">{list.data.total.toLocaleString("en")} total</span> : null}
      </PageTitle>
      <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_12rem]">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            className="pl-9"
            placeholder="Name, phone or area"
            aria-label={`Search ${contactLabel.toLowerCase()}`}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <Select aria-label="Opt-in" value={optIn} onChange={(e) => setOptIn(e.target.value as OptIn | "")}>
          <option value="">Any opt-in status</option>
          <option value="opted_in">Opted in</option>
          <option value="pending">Not yet opted in</option>
          <option value="opted_out">Opted out</option>
        </Select>
      </div>
      <ErrorNote error={list.error} />
      <Card className={cn("overflow-hidden transition-opacity", list.isPlaceholderData && list.isFetching && "opacity-60")}>
        {list.isPending ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : !list.data?.items.length ? (
          <Empty title={`No ${contactLabel.toLowerCase()} match`} />
        ) : (
          <ul className="divide-y divide-line">
            {list.data.items.map((c) => (
              <li key={c.id}>
                <button className="grid w-full grid-cols-[1fr_auto] gap-x-3 px-4 py-3 text-left hover:bg-line/30 md:grid-cols-[1.4fr_1fr_1fr_auto]" onClick={() => setOpenId(c.id)}>
                  <div className="min-w-0">
                    <p className="truncate font-medium">{c.name ?? phone(c.wa_id)}</p>
                    <p className="text-xs text-muted">{phone(c.wa_id)}</p>
                  </div>
                  <div className="text-right md:text-left">
                    <Badge tone={OPT_TONE[c.opt_in_status]}>{label(STATUS_LABEL, c.opt_in_status)}</Badge>
                  </div>
                  <p className="text-sm text-ink-2 md:self-center">{c.area ?? "No area"}</p>
                  <p className="text-right text-sm text-ink-2 md:self-center">{c.language ?? ""}</p>
                </button>
              </li>
            ))}
          </ul>
        )}
        {list.data && list.data.total > PAGE ? (
          <div className="flex items-center justify-between border-t border-line px-4 py-2 text-sm text-muted">
            <span>
              {page * PAGE + 1}–{Math.min((page + 1) * PAGE, list.data.total)} of {list.data.total}
            </span>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Previous
              </Button>
              <Button size="sm" variant="secondary" disabled={(page + 1) * PAGE >= list.data.total} onClick={() => setPage(page + 1)}>
                Next
              </Button>
            </div>
          </div>
        ) : null}
      </Card>
      <CustomerDrawer id={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}

function Evidence({ value }: { value: Record<string, unknown> | null }) {
  if (!value) return <p className="text-sm text-muted">No evidence recorded.</p>;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
      {Object.entries(value).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
          <dd className="break-words" dir="auto">
            {typeof v === "string" && /^\d{4}-\d\d-\d\dT/.test(v) ? dateTime(v) : typeof v === "object" ? JSON.stringify(v) : String(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function CustomerDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const modules = useModules();
  const client = useQueryClient();
  const canEdit = useCan("agent");
  const c = useQuery({ queryKey: ["customer", id], queryFn: () => api<CustomerDetail>(`/contacts/${id}`), enabled: id !== null });
  const [edit, setEdit] = useState<Record<string, string> | null>(null);
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<CustomerDetail>(`/contacts/${id}`, { method: "PATCH", body }),
    onSuccess: (d) => {
      client.setQueryData(["customer", id], d);
      void client.invalidateQueries({ queryKey: ["customers"] });
      setEdit(null);
    },
  });
  const d = c.data;

  return (
    <Sheet
      open={id !== null}
      onOpenChange={(v) => {
        if (!v) {
          setEdit(null);
          patch.reset();
          onClose();
        }
      }}
      wide
      title={d ? (d.name ?? phone(d.wa_id)) : "Customer"}
      description={d ? `${phone(d.wa_id)}${d.area ? ` · ${d.area}` : ""}` : undefined}
    >
      {!d ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-6">
          <ErrorNote error={patch.error} />
          <section>
            <SectionTitle>Details</SectionTitle>
            {edit ? (
              <div className="space-y-3">
                {(["name", "area", "emirate", "address_note"] as const).map((k) => (
                  <Field key={k} label={k === "address_note" ? "Address note" : k.charAt(0).toUpperCase() + k.slice(1)}>
                    <Input value={edit[k] ?? ""} onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
                  </Field>
                ))}
                <div className="flex gap-2">
                  <Button
                    disabled={patch.isPending}
                    onClick={() =>
                      patch.mutate(Object.fromEntries(Object.entries(edit).map(([k, v]) => [k, v.trim() || null])))
                    }
                  >
                    Save
                  </Button>
                  <Button variant="ghost" onClick={() => setEdit(null)}>
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex items-start justify-between gap-3">
                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                  <dt className="text-muted">Area</dt>
                  <dd>{d.area ?? "—"}</dd>
                  <dt className="text-muted">Emirate</dt>
                  <dd>{d.emirate ?? "—"}</dd>
                  <dt className="text-muted">Address</dt>
                  <dd>{d.address_note ?? "—"}</dd>
                  <dt className="text-muted">Language</dt>
                  <dd>{d.language ?? "—"}</dd>
                  <dt className="text-muted">Source</dt>
                  <dd>{d.source ?? "—"}</dd>
                </dl>
                {canEdit ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setEdit({ name: d.name ?? "", area: d.area ?? "", emirate: d.emirate ?? "", address_note: d.address_note ?? "" })}
                  >
                    Edit
                  </Button>
                ) : null}
              </div>
            )}
          </section>

          <section>
            <SectionTitle>Marketing opt-in</SectionTitle>
            <div className="mb-2 flex items-center justify-between gap-3">
              <Badge tone={OPT_TONE[d.opt_in_status]}>{label(STATUS_LABEL, d.opt_in_status)}</Badge>
              <span className="text-xs text-muted">
                {d.opt_in_status === "opted_in" && d.opt_in_at ? `since ${dateTime(d.opt_in_at)}` : null}
                {d.opt_in_status === "opted_out" && d.opt_out_at ? `since ${dateTime(d.opt_out_at)}` : null}
              </span>
            </div>
            {d.opt_in_status === "opted_in" ? <Evidence value={d.opt_in_evidence} /> : null}
            {d.opt_in_status === "pending" ? (
              <p className="text-sm text-muted">Only the customer can opt in (by replying on WhatsApp). Campaigns never reach them until then.</p>
            ) : null}
            {canEdit && d.opt_in_status !== "opted_out" ? (
              <Button
                size="sm"
                variant="danger"
                className="mt-3"
                disabled={patch.isPending}
                onClick={() => {
                  if (window.confirm("Stop all marketing messages to this customer?")) patch.mutate({ opt_out: true });
                }}
              >
                Record opt-out
              </Button>
            ) : null}
          </section>

          {modules.map((m) =>
            m.ContactPanel && d.modules[m.key] !== undefined ? (
              <m.ContactPanel key={m.key} data={d.modules[m.key]} contact={d} />
            ) : null,
          )}

          {d.conversations.length ? (
            <section>
              <SectionTitle>Chats</SectionTitle>
              <ul className="space-y-1 text-sm">
                {d.conversations.map((v) => (
                  <li key={v.id}>
                    <Link className="text-accent-ink" to={`/conversations/${v.id}`}>
                      {label(STATUS_LABEL, v.state)} · last message {v.last_inbound_at ? dateTime(v.last_inbound_at) : "—"}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </Sheet>
  );
}


function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">{children}</h3>;
}
