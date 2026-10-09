// Console admin: create and set up clients, their modules, WhatsApp numbers, Telegram bot and
// dashboard users, and HMH Labz's own staff. Everything is decided on the server (roles, rules,
// secrets); these screens only collect input and show results. Secrets travel one way: a Meta
// token or bot token is sent once and never shown again, a staff authenticator QR is shown once
// and never fetched again.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Check, Copy, Download, KeyRound, Phone, Plus, RefreshCw, ShieldCheck, UserPlus } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { ApiError } from "@/lib/api";
import { ago, dateLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { consoleApi, consoleDownload, STAFF_RANK, type StaffRole } from "./api";

// ---------------------------------------------------------------- types (api/platform/admin.py)

interface ModuleInfo {
  key: string;
  name: string;
  description: string;
  requires: string[];
}
interface Catalogue {
  modules: ModuleInfo[];
  presets: Record<string, string[]>;
  tenant_roles: string[];
  staff_roles: StaffRole[];
}
interface ChannelAdmin {
  id: string;
  kind: "whatsapp" | "telegram";
  phone_number_id: string | null;
  waba_id: string | null;
  display_phone: string | null;
  is_active: boolean;
  has_token: boolean;
  token_expires_at: string | null;
  quality_rating: string | null;
  telegram_username: string | null;
  webhook_set_at: string | null;
  webhook_error: string | null;
}
interface UserAdmin {
  id: string;
  email: string;
  name: string | null;
  role: string;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}
interface Profile {
  id: string;
  slug: string;
  name: string;
  legal_name: string | null;
  status: string;
  contract_ref: string | null;
  timezone: string;
  locale_default: string;
  service_start: string | null;
  free_months_until: string | null;
  meta_charges_borne_by_us_until: string | null;
  monthly_fee_aed: string | null;
  monthly_message_cap_aed: string | null;
  escalation_phone: string | null;
  contact_label: string | null;
  created_at: string;
}
interface TenantAdmin {
  profile: Profile;
  modules: string[];
  channels: ChannelAdmin[];
  users: UserAdmin[];
}
interface StaffOut {
  id: string;
  email: string;
  name: string | null;
  role: StaffRole;
  is_active: boolean;
  has_totp: boolean;
  created_at: string;
  last_login_at: string | null;
}
interface Enrolment {
  staff: StaffOut;
  otpauth_uri: string;
  qr_svg: string;
}

const A = "/admin";
const STATUSES = ["trial", "active", "suspended", "churned"] as const;
const PRESET_LABEL: Record<string, string> = {
  water_delivery: "Water delivery",
  real_estate: "Real estate",
  law_firm: "Law firm",
};

/** Plain-language versions of the server's error codes. */
const REASON: Record<string, string> = {
  slug_taken: "That address is already used by another client.",
  phone_number_taken: "That WhatsApp number is already connected to a client.",
  email_exists: "There is already an account with that email.",
  last_admin: "A client needs at least one active admin. Make someone else admin first.",
  last_owner: "There must always be one active owner.",
  not_yourself: "Ask another owner to change your own role or access.",
  meta_rejected: "Meta did not accept that token. Nothing was changed.",
  telegram_token_invalid: "That does not look like a bot token. Copy the whole line BotFather sent, like 123456789:AA…",
  telegram_rejected: "Telegram did not accept that token. Nothing was changed.",
  bot_taken: "That bot is already connected to a client.",
  different_bot: "That token is for a different bot. Use the new token of this same bot (BotFather → /revoke).",
  public_api_base_url_missing: "The server has no public address set (PUBLIC_API_BASE_URL), so Telegram cannot reach it.",
  channel_inactive: "Activate the bot first.",
  not_whatsapp: "That only applies to WhatsApp numbers.",
};

function explain(error: unknown): unknown {
  if (error instanceof ApiError && error.code && REASON[error.code]) {
    const message = (error.detail as { message?: string }).message;
    const extra = (error.code === "meta_rejected" || error.code === "telegram_rejected") && message ? ` (${message})` : "";
    return new Error(REASON[error.code] + extra);
  }
  if (error instanceof ApiError && error.code === "modules") {
    return new Error((error.detail as { message?: string }).message ?? "Those modules cannot be combined.");
  }
  return error;
}

function useCatalogue() {
  return useQuery({ queryKey: ["console", "catalogue"], queryFn: () => consoleApi<Catalogue>(`${A}/catalogue`), staleTime: Infinity });
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

/** A temporary password the person changes on first sign-in: 16 characters, no look-alikes. */
function tempPassword(): string {
  const chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

// ---------------------------------------------------------------- small pieces

/** An on/off switch: a real button with role="switch", the state in words for screen readers. */
function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "press relative inline-flex h-6 w-10 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50",
        checked ? "border-ink bg-ink" : "border-line bg-line/70",
      )}
    >
      <span
        className={cn(
          "inline-block size-4 rounded-full bg-page shadow-sm transition-transform duration-200 ease-out",
          checked ? "translate-x-[1.1rem]" : "translate-x-[0.2rem]",
        )}
      />
    </button>
  );
}

function Saved({ show }: { show: boolean }) {
  return (
    <span
      aria-live="polite"
      className={cn("inline-flex items-center gap-1 text-sm text-good transition-opacity duration-300", show ? "opacity-100" : "opacity-0")}
    >
      <Check className="size-4" aria-hidden /> Saved
    </span>
  );
}

function useFlash(): [boolean, () => void] {
  const [on, setOn] = useState(false);
  return [
    on,
    () => {
      setOn(true);
      window.setTimeout(() => setOn(false), 1800);
    },
  ];
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, flash] = useFlash();
  return (
    <Button
      size="sm"
      variant="secondary"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(flash);
      }}
    >
      {done ? <Check aria-hidden /> : <Copy aria-hidden />} {done ? "Copied" : label}
    </Button>
  );
}

/** Usage | Setup, as links so each has its own URL and the back button works. */
export function ClientTabs({ id }: { id: string }) {
  const tab = (to: string, label: string, end: boolean) => (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn("relative px-1 pb-2.5 text-sm transition-colors", isActive ? "font-medium text-ink" : "text-muted hover:text-ink")
      }
    >
      {({ isActive }) => (
        <>
          {label}
          <span
            aria-hidden
            className={cn(
              "absolute inset-x-0 -bottom-px h-0.5 origin-left rounded-full bg-accent transition-transform duration-300 ease-out",
              isActive ? "scale-x-100" : "scale-x-0",
            )}
          />
        </>
      )}
    </NavLink>
  );
  return (
    <nav aria-label="Client" className="mb-5 flex gap-6 border-b border-line">
      {tab(`/console/tenants/${id}`, "Usage", true)}
      {tab(`/console/tenants/${id}/setup`, "Setup", false)}
    </nav>
  );
}

// ---------------------------------------------------------------- new client

export function NewClientPage() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const cat = useCatalogue();
  const [f, setF] = useState({ name: "", slug: "", legal_name: "", status: "trial", preset: "", monthly_fee_aed: "" });
  const [slugTouched, setSlugTouched] = useState(false);
  const slug = slugTouched ? f.slug : slugify(f.name);
  const create = useMutation({
    mutationFn: () =>
      consoleApi<TenantAdmin>(`${A}/tenants`, {
        method: "POST",
        body: {
          name: f.name.trim(),
          slug,
          legal_name: f.legal_name.trim() || null,
          status: f.status,
          monthly_fee_aed: f.monthly_fee_aed || null,
          modules: f.preset ? [f.preset] : [],
        },
      }),
    onSuccess: (t) => {
      void client.invalidateQueries({ queryKey: ["console", "overview"] });
      navigate(`/console/tenants/${t.profile.id}/setup`);
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <div className="mx-auto max-w-2xl">
      <p className="mb-2 text-sm">
        <Link to="/console" className="text-muted hover:text-ink">
          ← Clients
        </Link>
      </p>
      <PageTitle title="New client" />
      <Card className="p-5 md:p-6">
        <form onSubmit={submit} className="space-y-5">
          <Field label="Business name">
            <Input required autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Acme Water" />
          </Field>
          <Field label="Dashboard address" hint="Lowercase letters, digits and dashes. Clients sign in here; it cannot be changed later.">
            <div className="flex items-center overflow-hidden rounded-lg border border-line bg-surface focus-within:ring-2 focus-within:ring-accent/40">
              <input
                className="h-10 min-w-0 flex-1 bg-transparent px-3 text-base outline-none md:text-sm"
                required
                value={slug}
                onChange={(e) => {
                  setSlugTouched(true);
                  setF({ ...f, slug: e.target.value.toLowerCase() });
                }}
                pattern="[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?"
                aria-label="Dashboard address"
              />
              <span className="pr-3 text-sm text-muted">.heyozo.com</span>
            </div>
          </Field>
          <Field label="Legal name" hint="As on the contract.">
            <Input value={f.legal_name} onChange={(e) => setF({ ...f, legal_name: e.target.value })} placeholder="Acme Water L.L.C" />
          </Field>
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Status">
              <Select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}>
                <option value="trial">Trial</option>
                <option value="active">Active</option>
              </Select>
            </Field>
            <Field label="Monthly fee (AED)" hint="Optional. Used for margin.">
              <Input inputMode="decimal" value={f.monthly_fee_aed} onChange={(e) => setF({ ...f, monthly_fee_aed: e.target.value })} placeholder="1500.00" />
            </Field>
          </div>
          <fieldset>
            <legend className="mb-2 text-sm font-medium text-ink-2">Start with</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {([["", "Nothing yet", "Switch modules on later, in Setup."], ...Object.entries(cat.data?.presets ?? {}).map(([k, v]) => [k, PRESET_LABEL[k] ?? k, v.join(", ")])] as [string, string, string][]).map(
                ([key, title, sub]) => (
                  <label
                    key={key}
                    className={cn(
                      "press flex cursor-pointer gap-3 rounded-lg border p-3 text-sm",
                      f.preset === key ? "border-ink bg-surface" : "border-line hover:border-ink-2/40",
                    )}
                  >
                    <input type="radio" name="preset" className="mt-0.5 accent-[rgb(var(--ink))]" checked={f.preset === key} onChange={() => setF({ ...f, preset: key })} />
                    <span>
                      <span className="block font-medium">{title}</span>
                      <span className="block text-muted">{sub}</span>
                    </span>
                  </label>
                ),
              )}
            </div>
          </fieldset>
          <ErrorNote error={explain(create.error)} />
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={() => navigate("/console")}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !f.name.trim() || !slug}>
              {create.isPending ? "Creating…" : "Create client"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------- client setup

export function ClientSetupPage({ role }: { role: StaffRole }) {
  const { id = "" } = useParams();
  const key = ["console", "admin", id];
  const q = useQuery({ queryKey: key, queryFn: () => consoleApi<TenantAdmin>(`${A}/tenants/${id}`) });
  const canEdit = STAFF_RANK[role] >= STAFF_RANK.ops;
  if (q.isPending)
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  if (q.isError) return <ErrorNote error={q.error} />;
  const t = q.data;
  return (
    <div>
      <PageTitle title={t.profile.name}>
        <Badge tone={t.profile.status === "active" ? "good" : t.profile.status === "trial" ? "accent" : "neutral"}>{t.profile.status}</Badge>
      </PageTitle>
      <ClientTabs id={id} />
      {!canEdit ? <p className="mb-4 text-sm text-muted">You can look; changes need the ops or owner role.</p> : null}
      <div className="enter-list space-y-4">
        <ProfileCard t={t} canEdit={canEdit} qk={key} />
        <ModulesCard t={t} canEdit={canEdit} qk={key} />
        <ChannelsCard t={t} canEdit={canEdit} qk={key} />
        <TelegramCard t={t} canEdit={canEdit} qk={key} />
        <UsersCard t={t} canEdit={canEdit} qk={key} />
        <DocumentsCard t={t} />
      </div>
    </div>
  );
}

type Qk = (string | number)[];

function useAdminMutation<V>(qk: Qk, fn: (v: V) => Promise<TenantAdmin>, onDone?: () => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (data) => {
      client.setQueryData(qk, data);
      void client.invalidateQueries({ queryKey: ["console", "overview"] });
      void client.invalidateQueries({ queryKey: ["console", "tenant", qk[2]] });
      onDone?.();
    },
  });
}

function ProfileCard({ t, canEdit, qk }: { t: TenantAdmin; canEdit: boolean; qk: Qk }) {
  const p = t.profile;
  const initial = useMemo(
    () => ({
      name: p.name,
      legal_name: p.legal_name ?? "",
      status: p.status,
      contract_ref: p.contract_ref ?? "",
      monthly_fee_aed: p.monthly_fee_aed ?? "",
      monthly_message_cap_aed: p.monthly_message_cap_aed ?? "",
      service_start: p.service_start ?? "",
      free_months_until: p.free_months_until ?? "",
      meta_charges_borne_by_us_until: p.meta_charges_borne_by_us_until ?? "",
      escalation_phone: p.escalation_phone ?? "",
      contact_label: p.contact_label ?? "",
      locale_default: p.locale_default,
      timezone: p.timezone,
    }),
    [p],
  );
  const [f, setF] = useState(initial);
  const [saved, flash] = useFlash();
  const changed = Object.fromEntries(
    Object.entries(f)
      .filter(([k, v]) => v !== initial[k as keyof typeof initial])
      .map(([k, v]) => [k, v === "" ? null : v]),
  );
  const dirty = Object.keys(changed).length > 0;
  const save = useAdminMutation(qk, () => consoleApi<TenantAdmin>(`${A}/tenants/${p.id}`, { method: "PATCH", body: changed }), flash);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Card>
      <CardHeader title="Profile" subtitle={`${p.slug}.heyozo.com · created ${dateLabel(p.created_at)}`} action={<Saved show={saved} />} />
      <form
        className="grid gap-4 p-4 sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(undefined);
        }}
      >
        <fieldset disabled={!canEdit} className="contents">
          <Field label="Business name">
            <Input required value={f.name} onChange={set("name")} />
          </Field>
          <Field label="Legal name">
            <Input value={f.legal_name} onChange={set("legal_name")} />
          </Field>
          <Field label="Status" hint="Suspended or churned clients stop receiving replies.">
            <Select value={f.status} onChange={set("status")}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {cap(s)}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Contract reference">
            <Input value={f.contract_ref} onChange={set("contract_ref")} placeholder="HMH-…" />
          </Field>
          <Field label="Monthly fee (AED)">
            <Input inputMode="decimal" value={f.monthly_fee_aed} onChange={set("monthly_fee_aed")} />
          </Field>
          <Field label="Monthly message cap (AED)" hint="Alerts as Meta charges approach it.">
            <Input inputMode="decimal" value={f.monthly_message_cap_aed} onChange={set("monthly_message_cap_aed")} />
          </Field>
          <Field label="Service start">
            <Input type="date" value={f.service_start} onChange={set("service_start")} />
          </Field>
          <Field label="Free until" hint="No fee is charged before this date.">
            <Input type="date" value={f.free_months_until} onChange={set("free_months_until")} />
          </Field>
          <Field label="HMH Labz pays Meta charges until">
            <Input type="date" value={f.meta_charges_borne_by_us_until} onChange={set("meta_charges_borne_by_us_until")} />
          </Field>
          <Field label="Escalation WhatsApp" hint="Digits only, with country code.">
            <Input inputMode="tel" value={f.escalation_phone} onChange={set("escalation_phone")} placeholder="9715…" />
          </Field>
          <Field label="What they call their contacts" hint="Shown in their dashboard, e.g. Customers, Clients, Patients.">
            <Input value={f.contact_label} onChange={set("contact_label")} placeholder="Customers" />
          </Field>
          <Field label="Default language">
            <Select value={f.locale_default} onChange={set("locale_default")}>
              <option value="en">English</option>
              <option value="ar">Arabic</option>
            </Select>
          </Field>
        </fieldset>
        {canEdit ? (
          <div className="flex flex-wrap items-center justify-end gap-3 sm:col-span-2">
            <div className="mr-auto min-w-0">
              <ErrorNote error={explain(save.error)} />
            </div>
            {dirty ? (
              <Button variant="ghost" onClick={() => setF(initial)}>
                Undo changes
              </Button>
            ) : null}
            <Button type="submit" disabled={!dirty || save.isPending}>
              {save.isPending ? "Saving…" : "Save profile"}
            </Button>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

function ModulesCard({ t, canEdit, qk }: { t: TenantAdmin; canEdit: boolean; qk: Qk }) {
  const cat = useCatalogue();
  const [on, setOn] = useState<Set<string>>(new Set(t.modules));
  const [saved, flash] = useFlash();
  const dirty = on.size !== t.modules.length || t.modules.some((m) => !on.has(m));
  const save = useAdminMutation(
    qk,
    () => consoleApi<TenantAdmin>(`${A}/tenants/${t.profile.id}/modules`, { method: "PUT", body: { enabled: [...on] } }),
    flash,
  );
  const modules = cat.data?.modules ?? [];
  const nameOf = (k: string) => modules.find((m) => m.key === k)?.name ?? k;
  function flip(m: ModuleInfo, value: boolean) {
    const next = new Set(on);
    if (value) {
      next.add(m.key);
      m.requires.forEach((r) => next.add(r)); // switching on brings what it needs
    } else {
      next.delete(m.key);
      modules.filter((x) => x.requires.includes(m.key)).forEach((x) => next.delete(x.key)); // and what needs it goes too
    }
    setOn(next);
  }
  return (
    <Card>
      <CardHeader title="Modules" subtitle="What the agent and the client's dashboard can do. Data is kept when a module is switched off." action={<Saved show={saved} />} />
      {cat.isPending ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : (
        <ul className="divide-y divide-line px-4">
          {modules.map((m) => (
            <li key={m.key} className="flex items-start justify-between gap-4 py-3">
              <div className="min-w-0">
                <p className="text-sm font-medium">{m.name}</p>
                <p className="text-sm text-muted">{m.description}</p>
                {m.requires.length ? <p className="mt-0.5 text-xs text-muted">Needs {m.requires.map(nameOf).join(", ")}</p> : null}
              </div>
              <Toggle checked={on.has(m.key)} onChange={(v) => flip(m, v)} label={m.name} disabled={!canEdit} />
            </li>
          ))}
        </ul>
      )}
      {canEdit ? (
        <div className="flex flex-wrap items-center justify-end gap-3 border-t border-line px-4 py-3">
          <div className="mr-auto min-w-0">
            <ErrorNote error={explain(save.error)} />
          </div>
          {dirty ? (
            <Button variant="ghost" onClick={() => setOn(new Set(t.modules))}>
              Undo
            </Button>
          ) : null}
          <Button disabled={!dirty || save.isPending} onClick={() => save.mutate(undefined)}>
            {save.isPending ? "Saving…" : "Save modules"}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

function tokenState(c: ChannelAdmin): { tone: "good" | "warn" | "bad"; text: string } {
  if (!c.has_token) return { tone: "bad", text: "No token" };
  if (c.token_expires_at) {
    const days = (new Date(c.token_expires_at).getTime() - Date.now()) / 86_400_000;
    if (days < 0) return { tone: "bad", text: "Token expired" };
    if (days < 14) return { tone: "warn", text: `Token expires ${dateLabel(c.token_expires_at)}` };
    return { tone: "good", text: `Token until ${dateLabel(c.token_expires_at)}` };
  }
  return { tone: "good", text: "Token set" };
}

function ChannelsCard({ t, canEdit, qk }: { t: TenantAdmin; canEdit: boolean; qk: Qk }) {
  const [adding, setAdding] = useState(false);
  const [tokenFor, setTokenFor] = useState<ChannelAdmin | null>(null);
  const toggle = useAdminMutation(qk, (c: ChannelAdmin) =>
    consoleApi<TenantAdmin>(`${A}/channels/${c.id}`, { method: "PATCH", body: { is_active: !c.is_active } }),
  );
  const numbers = t.channels.filter((c) => c.kind === "whatsapp");
  return (
    <Card>
      <CardHeader
        title="WhatsApp numbers"
        subtitle="The client's numbers on their own WhatsApp Business Account."
        action={
          canEdit ? (
            <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
              <Plus aria-hidden /> Add number
            </Button>
          ) : null
        }
      />
      {numbers.length === 0 ? (
        <Empty title="No number connected">Add the number's Phone number ID from Meta's WhatsApp Manager, then its token.</Empty>
      ) : (
        <ul className="divide-y divide-line px-4">
          {numbers.map((c) => {
            const tok = tokenState(c);
            return (
              <li key={c.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                <Phone className="size-4 text-muted" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{c.display_phone ?? "Number not named"}</p>
                  <p className="text-xs text-muted tabular">
                    ID {c.phone_number_id}
                    {c.waba_id ? ` · WABA ${c.waba_id}` : ""} · quality {c.quality_rating?.toLowerCase() ?? "unknown"}
                  </p>
                </div>
                <Badge tone={tok.tone}>{tok.text}</Badge>
                {!c.is_active ? <Badge>Inactive</Badge> : null}
                {canEdit ? (
                  <div className="flex gap-2">
                    <Button size="sm" variant="secondary" onClick={() => setTokenFor(c)}>
                      <KeyRound aria-hidden /> {c.has_token ? "Replace token" : "Add token"}
                    </Button>
                    <Button size="sm" variant="ghost" disabled={toggle.isPending} onClick={() => toggle.mutate(c)}>
                      {c.is_active ? "Deactivate" : "Activate"}
                    </Button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      <div className="px-4 pb-3">
        <ErrorNote error={explain(toggle.error)} />
      </div>
      <AddChannelSheet open={adding} onOpenChange={setAdding} tenantId={t.profile.id} qk={qk} />
      <TokenSheet channel={tokenFor} onClose={() => setTokenFor(null)} qk={qk} />
    </Card>
  );
}

function webhookState(c: ChannelAdmin): { tone: "good" | "warn" | "bad"; text: string } {
  if (!c.is_active) return { tone: "warn", text: "Webhook off" };
  if (c.webhook_error) return { tone: "bad", text: c.webhook_error };
  if (!c.webhook_set_at) return { tone: "bad", text: "Webhook not registered" };
  return { tone: "good", text: `Receiving since ${dateLabel(c.webhook_set_at)}` };
}

function TelegramCard({ t, canEdit, qk }: { t: TenantAdmin; canEdit: boolean; qk: Qk }) {
  const bots = t.channels.filter((c) => c.kind === "telegram");
  const [sheet, setSheet] = useState<{ bot: ChannelAdmin | null } | null>(null);
  const toggle = useAdminMutation(qk, (c: ChannelAdmin) =>
    consoleApi<TenantAdmin>(`${A}/channels/${c.id}`, { method: "PATCH", body: { is_active: !c.is_active } }),
  );
  const reregister = useAdminMutation(qk, (c: ChannelAdmin) =>
    consoleApi<TenantAdmin>(`${A}/channels/${c.id}/telegram-webhook`, { method: "POST" }),
  );
  return (
    <Card>
      <CardHeader
        title="Telegram bot"
        subtitle="The client's own bot, made with @BotFather. Customers press Start once; then the agent answers there too."
        action={
          canEdit && bots.length === 0 ? (
            <Button size="sm" variant="secondary" onClick={() => setSheet({ bot: null })}>
              <Plus aria-hidden /> Connect a bot
            </Button>
          ) : null
        }
      />
      {bots.length === 0 ? (
        <Empty title="No bot connected">
          In Telegram, the client opens @BotFather, sends /newbot and picks a name; BotFather replies with a token. Paste it here.
        </Empty>
      ) : (
        <ul className="divide-y divide-line px-4">
          {bots.map((c) => {
            const hook = webhookState(c);
            return (
              <li key={c.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                <Bot className="size-4 text-muted" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{c.telegram_username ? `@${c.telegram_username}` : "Bot"}</p>
                  {c.telegram_username ? <p className="text-xs text-muted">t.me/{c.telegram_username}</p> : null}
                </div>
                <Badge tone={hook.tone}>{hook.text}</Badge>
                {!c.is_active ? <Badge>Inactive</Badge> : null}
                {canEdit ? (
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" variant="secondary" onClick={() => setSheet({ bot: c })}>
                      <KeyRound aria-hidden /> Replace token
                    </Button>
                    {c.is_active ? (
                      <Button size="sm" variant="ghost" disabled={reregister.isPending} onClick={() => reregister.mutate(c)}>
                        <RefreshCw aria-hidden /> Re-register
                      </Button>
                    ) : null}
                    <Button size="sm" variant="ghost" disabled={toggle.isPending} onClick={() => toggle.mutate(c)}>
                      {c.is_active ? "Deactivate" : "Activate"}
                    </Button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      <div className="px-4 pb-3">
        <ErrorNote error={explain(toggle.error ?? reregister.error)} />
      </div>
      <BotTokenSheet target={sheet} onClose={() => setSheet(null)} tenantId={t.profile.id} qk={qk} />
    </Card>
  );
}

function BotTokenSheet({
  target,
  onClose,
  tenantId,
  qk,
}: {
  target: { bot: ChannelAdmin | null } | null;
  onClose: () => void;
  tenantId: string;
  qk: Qk;
}) {
  const [token, setToken] = useState("");
  const close = () => {
    setToken("");
    onClose();
  };
  const bot = target?.bot ?? null;
  const save = useAdminMutation(
    qk,
    () =>
      bot
        ? consoleApi<TenantAdmin>(`${A}/channels/${bot.id}/telegram-token`, { method: "PUT", body: { token: token.trim() } })
        : consoleApi<TenantAdmin>(`${A}/tenants/${tenantId}/telegram`, { method: "POST", body: { token: token.trim() } }),
    close,
  );
  return (
    <Sheet
      open={target !== null}
      onOpenChange={(v) => (v ? null : close())}
      title={bot ? "Replace the bot token" : "Connect a Telegram bot"}
      description={
        bot
          ? `For @${bot.telegram_username ?? ""}. After /revoke in BotFather. Checked with Telegram before it is saved.`
          : "Checked with Telegram before it is saved. The server then tells Telegram where to deliver messages."
      }
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button form="bot-token" type="submit" disabled={save.isPending || token.trim().length < 36}>
            {save.isPending ? "Checking with Telegram…" : bot ? "Save token" : "Check and connect"}
          </Button>
        </div>
      }
    >
      <form
        id="bot-token"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(undefined);
        }}
      >
        <Field label="Bot token from @BotFather" hint="Encrypted on the server and never shown again, here or anywhere.">
          <Input type="password" autoComplete="off" spellCheck={false} required value={token} onChange={(e) => setToken(e.target.value)} placeholder="123456789:AA…" />
        </Field>
        <ErrorNote error={explain(save.error)} />
      </form>
    </Sheet>
  );
}

function AddChannelSheet({ open, onOpenChange, tenantId, qk }: { open: boolean; onOpenChange: (v: boolean) => void; tenantId: string; qk: Qk }) {
  const [f, setF] = useState({ phone_number_id: "", waba_id: "", display_phone: "" });
  const add = useAdminMutation(
    qk,
    () =>
      consoleApi<TenantAdmin>(`${A}/tenants/${tenantId}/channels`, {
        method: "POST",
        body: { phone_number_id: f.phone_number_id.trim(), waba_id: f.waba_id.trim() || null, display_phone: f.display_phone.trim() || null },
      }),
    () => {
      onOpenChange(false);
      setF({ phone_number_id: "", waba_id: "", display_phone: "" });
    },
  );
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Add a WhatsApp number"
      description="From Meta WhatsApp Manager → Phone numbers. Add the token next."
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button form="add-channel" type="submit" disabled={add.isPending || !f.phone_number_id.trim()}>
            {add.isPending ? "Adding…" : "Add number"}
          </Button>
        </div>
      }
    >
      <form
        id="add-channel"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate(undefined);
        }}
      >
        <Field label="Phone number ID" hint="Digits only. Not the phone number itself.">
          <Input required inputMode="numeric" value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value })} />
        </Field>
        <Field label="WhatsApp Business Account ID">
          <Input inputMode="numeric" value={f.waba_id} onChange={(e) => setF({ ...f, waba_id: e.target.value })} />
        </Field>
        <Field label="Phone number, as people see it">
          <Input value={f.display_phone} onChange={(e) => setF({ ...f, display_phone: e.target.value })} placeholder="+971 5x xxx xxxx" />
        </Field>
        <ErrorNote error={explain(add.error)} />
      </form>
    </Sheet>
  );
}

function TokenSheet({ channel, onClose, qk }: { channel: ChannelAdmin | null; onClose: () => void; qk: Qk }) {
  const [token, setToken] = useState("");
  const [expires, setExpires] = useState("");
  const close = () => {
    setToken("");
    setExpires("");
    onClose();
  };
  const set = useAdminMutation(
    qk,
    () => consoleApi<TenantAdmin>(`${A}/channels/${channel?.id ?? ""}/token`, { method: "PUT", body: { token: token.trim(), expires: expires || null } }),
    close,
  );
  return (
    <Sheet
      open={channel !== null}
      onOpenChange={(v) => (v ? null : close())}
      title={channel?.has_token ? "Replace the token" : "Add the token"}
      description={`For ${channel?.display_phone ?? channel?.phone_number_id ?? ""}. Checked with Meta before it is saved.`}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button form="token" type="submit" disabled={set.isPending || token.trim().length < 20}>
            {set.isPending ? "Checking with Meta…" : "Save token"}
          </Button>
        </div>
      }
    >
      <form
        id="token"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          set.mutate(undefined);
        }}
      >
        <Field label="System-user access token" hint="Encrypted on the server and never shown again, here or anywhere.">
          <Input type="password" autoComplete="off" spellCheck={false} required value={token} onChange={(e) => setToken(e.target.value)} />
        </Field>
        <Field label="Expires on" hint="Leave empty for a token that does not expire.">
          <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
        <ErrorNote error={explain(set.error)} />
      </form>
    </Sheet>
  );
}

const ROLE_NOTE: Record<string, string> = {
  admin: "Everything, including team and settings",
  agent: "Conversations, orders and bookings",
  viewer: "Look only",
};

function UsersCard({ t, canEdit, qk }: { t: TenantAdmin; canEdit: boolean; qk: Qk }) {
  const [adding, setAdding] = useState(false);
  const [reset, setReset] = useState<{ user: UserAdmin; password: string } | null>(null);
  const [shown, setShown] = useState<{ email: string; password: string } | null>(null);
  const patch = useAdminMutation(qk, ({ id, body }: { id: string; body: Record<string, unknown> }) =>
    consoleApi<TenantAdmin>(`${A}/tenants/${t.profile.id}/users/${id}`, { method: "PATCH", body }),
  );
  return (
    <Card>
      <CardHeader
        title="Dashboard users"
        subtitle={`People who sign in at ${t.profile.slug}.heyozo.com`}
        action={
          canEdit ? (
            <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
              <UserPlus aria-hidden /> Add user
            </Button>
          ) : null
        }
      />
      {t.users.length === 0 ? (
        <Empty title="Nobody can sign in yet">Add the client's first admin; they can invite the rest of their team.</Empty>
      ) : (
        <ul className="divide-y divide-line px-4">
          {t.users.map((u) => (
            <li key={u.id} className={cn("flex flex-wrap items-center gap-x-4 gap-y-2 py-3", !u.is_active && "opacity-60")}>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{u.name ?? u.email}</p>
                <p className="truncate text-xs text-muted">
                  {u.name ? `${u.email} · ` : ""}
                  {u.last_login_at ? `signed in ${ago(u.last_login_at)}` : "never signed in"}
                </p>
              </div>
              {canEdit ? (
                <>
                  <Select
                    aria-label={`Role for ${u.email}`}
                    className="h-9 w-auto"
                    value={u.role}
                    disabled={patch.isPending}
                    onChange={(e) => patch.mutate({ id: u.id, body: { role: e.target.value } })}
                    title={ROLE_NOTE[u.role]}
                  >
                    {["admin", "agent", "viewer"].map((r) => (
                      <option key={r} value={r}>
                        {cap(r)}
                      </option>
                    ))}
                  </Select>
                  <Button size="sm" variant="ghost" onClick={() => setReset({ user: u, password: tempPassword() })}>
                    Reset password
                  </Button>
                  <Button size="sm" variant="ghost" disabled={patch.isPending} onClick={() => patch.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                    {u.is_active ? "Disable" : "Enable"}
                  </Button>
                </>
              ) : (
                <Badge>{u.role}</Badge>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="px-4 pb-3">
        <ErrorNote error={explain(patch.error)} />
      </div>
      <AddUserSheet open={adding} onOpenChange={setAdding} tenantId={t.profile.id} qk={qk} onCreated={setShown} />
      <Sheet
        open={reset !== null}
        onOpenChange={(v) => (v ? null : setReset(null))}
        title="Reset password"
        description={`${reset?.user.email ?? ""} is signed out everywhere and uses this new password next time.`}
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setReset(null)}>
              Cancel
            </Button>
            <Button
              disabled={patch.isPending}
              onClick={() => {
                if (!reset) return;
                patch.mutate(
                  { id: reset.user.id, body: { password: reset.password } },
                  {
                    onSuccess: () => {
                      setShown({ email: reset.user.email, password: reset.password });
                      setReset(null);
                    },
                  },
                );
              }}
            >
              Reset password
            </Button>
          </div>
        }
      >
        <p className="text-sm text-ink-2">A new temporary password will be set. You will see it once, to pass on securely.</p>
      </Sheet>
      <Sheet open={shown !== null} onOpenChange={(v) => (v ? null : setShown(null))} title="Pass this on securely" description="Shown once. Ask them to change it after signing in.">
        {shown ? (
          <div className="space-y-4">
            <dl className="space-y-3 rounded-lg border border-line bg-page p-4 text-sm">
              <div>
                <dt className="text-xs text-muted">Sign in at</dt>
                <dd className="font-medium">https://{t.profile.slug}.heyozo.com</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Email</dt>
                <dd className="font-medium">{shown.email}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Temporary password</dt>
                <dd className="font-mono text-base tracking-wide">{shown.password}</dd>
              </div>
            </dl>
            <div className="flex justify-between gap-2">
              <CopyButton text={`https://${t.profile.slug}.heyozo.com\n${shown.email}\n${shown.password}`} label="Copy all" />
              <Button onClick={() => setShown(null)}>Done</Button>
            </div>
          </div>
        ) : null}
      </Sheet>
    </Card>
  );
}

function AddUserSheet({
  open,
  onOpenChange,
  tenantId,
  qk,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  tenantId: string;
  qk: Qk;
  onCreated: (v: { email: string; password: string }) => void;
}) {
  const [f, setF] = useState({ email: "", name: "", role: "admin" });
  const [password] = useState(tempPassword);
  const add = useAdminMutation(
    qk,
    () =>
      consoleApi<TenantAdmin>(`${A}/tenants/${tenantId}/users`, {
        method: "POST",
        body: { email: f.email.trim(), name: f.name.trim() || null, role: f.role, password },
      }),
    () => {
      onOpenChange(false);
      onCreated({ email: f.email.trim().toLowerCase(), password });
      setF({ email: "", name: "", role: "admin" });
    },
  );
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Add a dashboard user"
      description="They get a temporary password, shown to you once."
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button form="add-user" type="submit" disabled={add.isPending || !f.email.includes("@")}>
            {add.isPending ? "Adding…" : "Add user"}
          </Button>
        </div>
      }
    >
      <form
        id="add-user"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate(undefined);
        }}
      >
        <Field label="Email">
          <Input type="email" required autoComplete="off" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        </Field>
        <Field label="Name">
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        </Field>
        <fieldset>
          <legend className="mb-2 text-sm font-medium text-ink-2">Role</legend>
          <div className="space-y-2">
            {["admin", "agent", "viewer"].map((r) => (
              <label key={r} className={cn("press flex cursor-pointer gap-3 rounded-lg border p-3 text-sm", f.role === r ? "border-ink" : "border-line")}>
                <input type="radio" name="role" className="mt-0.5 accent-[rgb(var(--ink))]" checked={f.role === r} onChange={() => setF({ ...f, role: r })} />
                <span>
                  <span className="block font-medium">{cap(r)}</span>
                  <span className="block text-muted">{ROLE_NOTE[r]}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <ErrorNote error={explain(add.error)} />
      </form>
    </Sheet>
  );
}

function DocumentsCard({ t }: { t: TenantAdmin }) {
  const dl = useMutation({ mutationFn: () => consoleDownload(`${A}/tenants/${t.profile.id}/dpa`, `dpa-${t.profile.slug}.md`) });
  return (
    <Card>
      <CardHeader title="Documents" />
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-4 pt-2">
        <p className="max-w-prose text-sm text-muted">
          Data processing description, generated from this client's live setup: modules, number and sub-processors. Review it, then send it with the contract.
        </p>
        <Button variant="secondary" disabled={dl.isPending} onClick={() => dl.mutate()}>
          <Download aria-hidden /> {dl.isPending ? "Preparing…" : "Download DPA"}
        </Button>
      </div>
      <div className="px-4 pb-3">
        <ErrorNote error={dl.error} />
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- staff (owner only)

export function StaffPage({ me }: { me: string }) {
  const client = useQueryClient();
  const qk = ["console", "staff"];
  const q = useQuery({ queryKey: qk, queryFn: () => consoleApi<StaffOut[]>(`${A}/staff`) });
  const [adding, setAdding] = useState(false);
  const [enrol, setEnrol] = useState<Enrolment | null>(null);
  const [resetFor, setResetFor] = useState<StaffOut | null>(null);
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) => consoleApi<StaffOut>(`${A}/staff/${id}`, { method: "PATCH", body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: qk }),
  });
  const totp = useMutation({
    mutationFn: (id: string) => consoleApi<Enrolment>(`${A}/staff/${id}/totp`, { method: "POST" }),
    onSuccess: (e) => {
      setResetFor(null);
      setEnrol(e);
      void client.invalidateQueries({ queryKey: qk });
    },
  });
  return (
    <div>
      <PageTitle title="Staff">
        <Button onClick={() => setAdding(true)}>
          <UserPlus aria-hidden /> Add staff
        </Button>
      </PageTitle>
      <p className="mb-4 max-w-prose text-sm text-muted">
        HMH Labz people who use this console. Support can look; ops can change clients; owners also manage staff and record reimbursements.
      </p>
      {q.isPending ? (
        <div className="flex justify-center py-16">
          <Spinner />
        </div>
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : (
        <Card>
          <ul className="enter-list divide-y divide-line px-4">
            {q.data.map((s) => (
              <li key={s.id} className={cn("flex flex-wrap items-center gap-x-4 gap-y-2 py-3", !s.is_active && "opacity-60")}>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">
                    {s.name ?? s.email} {s.email === me ? <span className="text-muted">(you)</span> : null}
                  </p>
                  <p className="truncate text-xs text-muted">
                    {s.name ? `${s.email} · ` : ""}
                    {s.last_login_at ? `signed in ${ago(s.last_login_at)}` : "never signed in"}
                    {s.has_totp ? "" : " · no authenticator"}
                  </p>
                </div>
                <Select
                  aria-label={`Role for ${s.email}`}
                  className="h-9 w-auto"
                  value={s.role}
                  disabled={patch.isPending}
                  onChange={(e) => patch.mutate({ id: s.id, body: { role: e.target.value } })}
                >
                  <option value="support">Support</option>
                  <option value="ops">Ops</option>
                  <option value="owner">Owner</option>
                </Select>
                <Button size="sm" variant="ghost" onClick={() => setResetFor(s)}>
                  <ShieldCheck aria-hidden /> New authenticator
                </Button>
                <Button size="sm" variant="ghost" disabled={patch.isPending} onClick={() => patch.mutate({ id: s.id, body: { is_active: !s.is_active } })}>
                  {s.is_active ? "Disable" : "Enable"}
                </Button>
              </li>
            ))}
          </ul>
          <div className="px-4 pb-3">
            <ErrorNote error={explain(patch.error)} />
          </div>
        </Card>
      )}
      <AddStaffSheet open={adding} onOpenChange={setAdding} onEnrolled={setEnrol} />
      <Sheet
        open={resetFor !== null}
        onOpenChange={(v) => (v ? null : setResetFor(null))}
        title="Replace the authenticator"
        description={`${resetFor?.email ?? ""}'s current codes stop working at once. They scan a new QR code.`}
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setResetFor(null)}>
              Cancel
            </Button>
            <Button disabled={totp.isPending} onClick={() => resetFor && totp.mutate(resetFor.id)}>
              {totp.isPending ? "Replacing…" : "Replace authenticator"}
            </Button>
          </div>
        }
      >
        <p className="text-sm text-ink-2">Use this when someone loses or replaces their phone.</p>
        <ErrorNote error={explain(totp.error)} />
      </Sheet>
      <EnrolmentSheet enrol={enrol} onClose={() => setEnrol(null)} />
    </div>
  );
}

function AddStaffSheet({ open, onOpenChange, onEnrolled }: { open: boolean; onOpenChange: (v: boolean) => void; onEnrolled: (e: Enrolment) => void }) {
  const client = useQueryClient();
  const [f, setF] = useState({ email: "", name: "", role: "support", password: "" });
  const add = useMutation({
    mutationFn: () => consoleApi<Enrolment>(`${A}/staff`, { method: "POST", body: { ...f, email: f.email.trim(), name: f.name.trim() || null } }),
    onSuccess: (e) => {
      void client.invalidateQueries({ queryKey: ["console", "staff"] });
      onOpenChange(false);
      setF({ email: "", name: "", role: "support", password: "" });
      onEnrolled(e);
    },
  });
  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Add a staff member"
      description="They sign in with this password and an authenticator app."
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button form="add-staff" type="submit" disabled={add.isPending || f.password.length < 12 || !f.email.includes("@")}>
            {add.isPending ? "Adding…" : "Add and show QR"}
          </Button>
        </div>
      }
    >
      <form
        id="add-staff"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <Field label="Email">
          <Input type="email" required autoComplete="off" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        </Field>
        <Field label="Name">
          <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        </Field>
        <Field label="Role">
          <Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            <option value="support">Support · look only</option>
            <option value="ops">Ops · change clients</option>
            <option value="owner">Owner · everything</option>
          </Select>
        </Field>
        <Field label="Password" hint="At least 12 characters. Agree it with them in person or by phone, never by chat.">
          <Input type="password" autoComplete="new-password" required minLength={12} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        </Field>
        <ErrorNote error={explain(add.error)} />
      </form>
    </Sheet>
  );
}

function EnrolmentSheet({ enrol, onClose }: { enrol: Enrolment | null; onClose: () => void }) {
  return (
    <Sheet
      open={enrol !== null}
      onOpenChange={(v) => (v ? null : onClose())}
      title="Scan with an authenticator app"
      description="Shown once. Closing this hides it for good."
      footer={
        <div className="flex justify-end">
          <Button onClick={onClose}>It's scanned</Button>
        </div>
      }
    >
      {enrol ? (
        <div className="space-y-4">
          <p className="text-sm text-ink-2">
            {enrol.staff.email} opens Google Authenticator, Authy or 1Password, chooses <b>Add</b> → <b>Scan QR code</b>, and scans this.
          </p>
          <div className="enter mx-auto w-fit rounded-xl border border-line bg-white p-3">
            <img src={enrol.qr_svg} alt="Authenticator QR code" className="size-56" />
          </div>
          <details className="text-sm">
            <summary className="cursor-pointer text-muted">Can't scan? Enter it by hand</summary>
            <p className="mt-2 break-all rounded-lg border border-line bg-page p-3 font-mono text-xs">{enrol.otpauth_uri}</p>
          </details>
        </div>
      ) : null}
    </Sheet>
  );
}
