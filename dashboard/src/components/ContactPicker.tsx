// Find or add a contact (customer / client / lead): used by every "new …" sheet that needs one.
import { useMutation, useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { ErrorNote } from "@/components/ui/misc";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { contactHandle } from "@/lib/format";
import type { CustomerDetail, CustomerRow, Page } from "@/lib/types";

/** "customer", "client", "lead" — the singular of the tenant's contact label, lower case. */
export function useContactNoun(): string {
  const plural = useAuth().session?.tenant.contact_label ?? "Customers";
  return plural.toLowerCase().replace(/s$/, "");
}

export function ContactPicker({
  value,
  onChange,
}: {
  value: CustomerRow | CustomerDetail | null;
  onChange: (c: CustomerRow | CustomerDetail | null) => void;
}) {
  const noun = useContactNoun();
  const [q, setQ] = useState("");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({
    wa_id: "",
    name: "",
    area: "",
    address_note: "",
  });
  const results = useQuery({
    queryKey: ["customers", { q, limit: 6 }],
    queryFn: () => api<Page<CustomerRow>>("/contacts", { query: { q, limit: 6 } }),
    enabled: q.trim().length >= 2 && !value,
  });
  const add = useMutation({
    mutationFn: () =>
      api<CustomerDetail>("/contacts", {
        method: "POST",
        body: {
          wa_id: draft.wa_id,
          name: draft.name || null,
          area: draft.area || null,
          address_note: draft.address_note || null,
        },
      }),
    onSuccess: (c) => {
      setAdding(false);
      onChange(c);
    },
  });

  if (value) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-lg border border-line px-3 py-2.5">
        <div className="min-w-0 text-sm">
          <p className="truncate font-medium">{value.name ?? contactHandle(value)}</p>
          <p className="text-xs text-muted">
            {contactHandle(value)}
            {value.area ? ` · ${value.area}` : ""}
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={() => onChange(null)}>
          Change
        </Button>
      </div>
    );
  }
  if (adding) {
    const existing = add.error instanceof ApiError && add.error.code === "customer_exists";
    return (
      <section className="space-y-3 rounded-lg border border-line p-3">
        <h3 className="text-sm font-medium">New {noun}</h3>
        <Field label="WhatsApp number" hint="Local numbers like 050 123 4567 are fine">
          <Input inputMode="tel" autoComplete="off" value={draft.wa_id} onChange={(e) => setDraft({ ...draft, wa_id: e.target.value })} />
        </Field>
        <Field label="Name">
          <Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Area">
            <Input value={draft.area} onChange={(e) => setDraft({ ...draft, area: e.target.value })} />
          </Field>
          <Field label="Address note">
            <Input value={draft.address_note} onChange={(e) => setDraft({ ...draft, address_note: e.target.value })} />
          </Field>
        </div>
        {existing ? (
          <p className="text-sm text-ink-2">
            That number is already on your list.{" "}
            <button
              className="font-medium text-accent-ink"
              onClick={async () => {
                const id = (add.error as ApiError).detail as { id: string };
                onChange(await api<CustomerDetail>(`/contacts/${id.id}`));
                setAdding(false);
              }}
            >
              Use them
            </button>
          </p>
        ) : (
          <ErrorNote error={add.error} />
        )}
        <div className="flex gap-2">
          <Button disabled={!draft.wa_id || add.isPending} onClick={() => add.mutate()}>
            Add {noun}
          </Button>
          <Button variant="ghost" onClick={() => setAdding(false)}>
            Back
          </Button>
        </div>
      </section>
    );
  }
  return (
    <section className="space-y-2">
      <Field label={noun.charAt(0).toUpperCase() + noun.slice(1)}>
        <Input placeholder="Search name or phone" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
      </Field>
      {results.data?.items.length ? (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {results.data.items.map((c) => (
            <li key={c.id}>
              <button className="w-full px-3 py-2 text-left text-sm hover:bg-line/30" onClick={() => onChange(c)}>
                <span className="font-medium">{c.name ?? contactHandle(c)}</span>
                <span className="ml-2 text-xs text-muted">
                  {contactHandle(c)}
                  {c.area ? ` · ${c.area}` : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : q.trim().length >= 2 && results.isSuccess ? (
        <p className="text-sm text-muted">No {noun} found.</p>
      ) : null}
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          setDraft({
            ...draft,
            wa_id: /\d{6,}/.test(q) ? q : "",
            name: /\d/.test(q) ? "" : q,
          });
          setAdding(true);
        }}
      >
        <Plus /> New {noun}
      </Button>
    </section>
  );
}
