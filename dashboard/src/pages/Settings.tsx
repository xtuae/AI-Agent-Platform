import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api, setSession, type Role, type Session } from "@/lib/api";
import { useAuth, useCan } from "@/lib/auth";
import { aed, ago, dateLabel } from "@/lib/format";
import type { Product, Settings, TeamMember } from "@/lib/types";

const DAYS = [
  ["sat", "Saturday"],
  ["sun", "Sunday"],
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
] as const;

export default function SettingsPage() {
  const isAdmin = useCan("admin");
  return (
    <div className="space-y-4">
      <PageTitle title="Settings" />
      {!isAdmin ? <p className="text-sm text-muted">Only an admin can change these settings.</p> : null}
      <BusinessSettings />
      <Products />
      {isAdmin ? <Team /> : null}
      <Password />
    </div>
  );
}

function Saved({ show }: { show: boolean }) {
  return show ? <span className="text-sm text-good">Saved</span> : null;
}

function BusinessSettings() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const s = useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("/settings") });
  const [hours, setHours] = useState<Record<string, { closed: boolean; from: string; to: string }>>({});
  const [escalation, setEscalation] = useState("");
  useEffect(() => {
    if (!s.data) return;
    const h: typeof hours = {};
    for (const [k] of DAYS) {
      const v = s.data.business_hours[k];
      const m = v ? /^(\d\d:\d\d)-(\d\d:\d\d)$/.exec(v) : null;
      h[k] = { closed: v === "closed", from: m?.[1] ?? "08:00", to: m?.[2] ?? "22:00" };
    }
    setHours(h);
    setEscalation(s.data.escalation_phone ?? "");
  }, [s.data]);
  const save = useMutation({
    mutationFn: () =>
      api<Settings>("/settings", {
        method: "PATCH",
        body: {
          business_hours: Object.fromEntries(Object.entries(hours).map(([k, v]) => [k, v.closed ? "closed" : `${v.from}-${v.to}`])),
          escalation_phone: escalation.trim() || null,
        },
      }),
    onSuccess: (d) => client.setQueryData(["settings"], d),
  });

  if (!s.data) return <Card className="p-6">{s.isError ? <ErrorNote error={s.error} /> : <Spinner />}</Card>;
  return (
    <Card>
      <CardHeader
        title="Business"
        subtitle={s.data.meta_charges_borne_by_us_until ? `WhatsApp charges borne by HMH Labz until ${dateLabel(s.data.meta_charges_borne_by_us_until)}` : undefined}
      />
      <form
        className="space-y-5 p-4"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={!isAdmin} className="space-y-2">
          <legend className="mb-1 text-sm font-medium text-ink-2">Opening hours · the agent tells customers these</legend>
          {DAYS.map(([k, name]) => {
            const v = hours[k];
            if (!v) return null;
            return (
              <div key={k} className="grid grid-cols-[6.5rem_auto_1fr] items-center gap-2 text-sm">
                <span>{name}</span>
                <label className="flex items-center gap-1.5 text-ink-2">
                  <input type="checkbox" className="size-4" checked={v.closed} onChange={(e) => setHours({ ...hours, [k]: { ...v, closed: e.target.checked } })} />
                  Closed
                </label>
                {v.closed ? (
                  <span />
                ) : (
                  <div className="flex items-center gap-1.5">
                    <Input type="time" aria-label={`${name} opens`} className="h-9 w-auto" value={v.from} onChange={(e) => setHours({ ...hours, [k]: { ...v, from: e.target.value } })} />
                    <span className="text-muted">–</span>
                    <Input type="time" aria-label={`${name} closes`} className="h-9 w-auto" value={v.to} onChange={(e) => setHours({ ...hours, [k]: { ...v, to: e.target.value } })} />
                  </div>
                )}
              </div>
            );
          })}
        </fieldset>
        <Field label="Escalation number" hint="Gets a WhatsApp alert when the agent hands a chat to a person">
          <Input inputMode="tel" disabled={!isAdmin} value={escalation} onChange={(e) => setEscalation(e.target.value)} placeholder="050 123 4567" />
        </Field>
        <p className="text-sm text-ink-2">
          Monthly message cap: {s.data.monthly_message_cap_aed ? aed(s.data.monthly_message_cap_aed) : "none"}{" "}
          <span className="text-muted">(set by HMH Labz)</span>
        </p>
        <ErrorNote error={save.error} />
        {isAdmin ? (
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={save.isPending}>
              Save
            </Button>
            <Saved show={save.isSuccess} />
          </div>
        ) : null}
      </form>
    </Card>
  );
}

function Products() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const products = useQuery({ queryKey: ["products", "all"], queryFn: () => api<Product[]>("/products") });
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  return (
    <Card>
      <CardHeader
        title="Products and prices"
        subtitle="The agent and every new order use these prices"
        action={
          isAdmin ? (
            <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
              <Plus /> Add
            </Button>
          ) : null
        }
      />
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {(products.data ?? []).map((p) => (
          <li key={p.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{p.name_en ?? p.sku}</p>
              <p className="text-xs text-muted">
                {p.sku} · {p.category ?? "—"}
                {p.stock_note ? ` · ${p.stock_note}` : ""}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {!p.is_active ? <Badge>Hidden</Badge> : null}
              <span className="text-sm tabular">{aed(p.price_aed)}</span>
              {isAdmin ? (
                <Button size="sm" variant="ghost" onClick={() => setEditing(p)}>
                  Edit
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {products.isPending ? <div className="p-4"><Spinner /></div> : null}
      {editing ? (
        <ProductSheet
          product={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void client.invalidateQueries({ queryKey: ["products"] });
          }}
        />
      ) : null}
    </Card>
  );
}

function ProductSheet({ product, onClose, onSaved }: { product: Product | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    sku: product?.sku ?? "",
    name_en: product?.name_en ?? "",
    name_ar: product?.name_ar ?? "",
    category: product?.category ?? "water",
    price_aed: product?.price_aed ?? "",
    stock_note: product?.stock_note ?? "",
    is_active: product?.is_active ?? true,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        name_en: f.name_en,
        name_ar: f.name_ar || null,
        category: f.category,
        price_aed: f.price_aed,
        stock_note: f.stock_note || null,
        is_active: f.is_active,
      };
      return product
        ? api<Product>(`/products/${product.id}`, { method: "PATCH", body })
        : api<Product>("/products", { method: "POST", body: { ...body, sku: f.sku } });
    },
    onSuccess: onSaved,
  });
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      title={product ? `Edit ${product.name_en ?? product.sku}` : "Add product"}
      footer={
        <Button className="w-full" disabled={save.isPending || !f.name_en || !f.price_aed || (!product && !f.sku)} onClick={() => save.mutate()}>
          Save
        </Button>
      }
    >
      <div className="space-y-4">
        {!product ? (
          <Field label="SKU" hint="Letters, numbers, dot, dash. Cannot be changed later.">
            <Input value={f.sku} onChange={(e) => setF({ ...f, sku: e.target.value })} />
          </Field>
        ) : null}
        <Field label="Name (English)">
          <Input value={f.name_en} onChange={(e) => setF({ ...f, name_en: e.target.value })} />
        </Field>
        <Field label="Name (Arabic)">
          <Input dir="rtl" value={f.name_ar} onChange={(e) => setF({ ...f, name_ar: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Category" hint="Coupon books pay for water only">
            <Select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
              <option value="water">Water</option>
              <option value="snack">Snack</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Price (AED)">
            <Input inputMode="decimal" value={f.price_aed} onChange={(e) => setF({ ...f, price_aed: e.target.value.replace(/[^\d.]/g, "") })} />
          </Field>
        </div>
        <Field label="Stock note" hint="Optional, e.g. 'back on Monday'">
          <Input value={f.stock_note} onChange={(e) => setF({ ...f, stock_note: e.target.value })} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="size-4" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} />
          Available to order
        </label>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}

const ROLE_HELP: Record<Role, string> = {
  viewer: "Can see everything, change nothing",
  agent: "Takes orders and chats with customers",
  admin: "Also changes settings, prices and team",
};

function Team() {
  const client = useQueryClient();
  const { session } = useAuth();
  const team = useQuery({ queryKey: ["team"], queryFn: () => api<TeamMember[]>("/team") });
  const [adding, setAdding] = useState(false);
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) => api<TeamMember>(`/team/${id}`, { method: "PATCH", body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["team"] }),
  });
  return (
    <Card>
      <CardHeader
        title="Team logins"
        action={
          <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
            <Plus /> Add
          </Button>
        }
      />
      <div className="px-4 pt-2">
        <ErrorNote error={update.error} />
      </div>
      <ul className="mt-2 divide-y divide-line border-t border-line">
        {(team.data ?? []).map((m) => (
          <li key={m.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">
                {m.name ?? m.email} {m.id === session?.user.id ? <span className="text-muted">(you)</span> : null}
              </p>
              <p className="truncate text-xs text-muted">
                {m.email} · {m.last_login_at ? `last in ${ago(m.last_login_at)}` : "never signed in"}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {m.is_active ? (
                <Select
                  aria-label={`Role of ${m.email}`}
                  className="h-8 w-auto"
                  value={m.role}
                  disabled={update.isPending}
                  onChange={(e) => update.mutate({ id: m.id, body: { role: e.target.value } })}
                >
                  <option value="viewer">Viewer</option>
                  <option value="agent">Agent</option>
                  <option value="admin">Admin</option>
                </Select>
              ) : (
                <Badge>Deactivated</Badge>
              )}
              {m.id !== session?.user.id ? (
                <Button
                  size="sm"
                  variant={m.is_active ? "ghost" : "secondary"}
                  disabled={update.isPending}
                  onClick={() => {
                    if (!m.is_active || window.confirm(`Sign ${m.email} out everywhere and block their login?`))
                      update.mutate({ id: m.id, body: { is_active: !m.is_active } });
                  }}
                >
                  {m.is_active ? "Deactivate" : "Reactivate"}
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {adding ? (
        <AddMember
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            void client.invalidateQueries({ queryKey: ["team"] });
          }}
        />
      ) : null}
    </Card>
  );
}

function AddMember({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ email: "", name: "", role: "agent" as Role, password: "" });
  const save = useMutation({ mutationFn: () => api<TeamMember>("/team", { method: "POST", body: { ...f, name: f.name || null } }), onSuccess: onSaved });
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      title="Add a team login"
      description="Share the temporary password with them in person; they can change it under Settings."
      footer={
        <Button className="w-full" disabled={save.isPending || !f.email || f.password.length < 12} onClick={() => save.mutate()}>
          Add login
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Email">
          <Input type="email" autoComplete="off" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        </Field>
        <Field label="Name">
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        </Field>
        <Field label="Role" hint={ROLE_HELP[f.role]}>
          <Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as Role })}>
            <option value="viewer">Viewer</option>
            <option value="agent">Agent</option>
            <option value="admin">Admin</option>
          </Select>
        </Field>
        <Field label="Temporary password" hint="At least 12 characters">
          <Input type="text" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        </Field>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}

function Password() {
  const { session } = useAuth();
  const [f, setF] = useState({ current_password: "", new_password: "" });
  const save = useMutation({
    mutationFn: () => api<Session>("/auth/password", { method: "POST", body: f }),
    onSuccess: (s) => {
      setSession(s);
      setF({ current_password: "", new_password: "" });
    },
  });
  return (
    <Card>
      <CardHeader title="Your password" subtitle={session ? `Signed in as ${session.user.email}` : undefined} />
      <form
        className="grid gap-3 p-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Current password">
          <Input type="password" autoComplete="current-password" value={f.current_password} onChange={(e) => setF({ ...f, current_password: e.target.value })} />
        </Field>
        <Field label="New password" hint="At least 12 characters; signs out your other devices">
          <Input type="password" autoComplete="new-password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} />
        </Field>
        <Button type="submit" disabled={save.isPending || f.new_password.length < 12 || !f.current_password}>
          Change
        </Button>
        <div className="sm:col-span-3">
          <ErrorNote error={save.error} />
          <Saved show={save.isSuccess} />
        </div>
      </form>
    </Card>
  );
}
