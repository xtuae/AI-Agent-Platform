// The HMH Labz platform console (admin.heyozo.com, or /console locally). Loaded as its own chunk:
// a tenant dashboard never downloads it. Every figure comes from the server; the console only
// formats. Cross-tenant data is read tenant by tenant under RLS on the server.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, LogOut, Plus, ShieldCheck, Wallet } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { BrowserRouter, Link, Navigate, NavLink, Outlet, Route, Routes, useLocation, useParams } from "react-router";
import { StatementView } from "@/components/Statement";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner, StatusDot } from "@/components/ui/misc";
import { Tile } from "@/components/ui/tile";
import { aed, ago, dateLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { consoleApi, onStaffSession, refreshStaff, STAFF_RANK, staffLogin, staffLogout, type StaffRole, type StaffSession } from "./api";
import { Logo } from "@/components/Logo";
import { ClientSetupPage, ClientTabs, NewClientPage, StaffPage } from "./admin";
import type { LedgerRow, Overview, TenantDetail, TenantRow } from "./types";

function useStaff(): { session: StaffSession | null; ready: boolean } {
  const client = useQueryClient();
  const [state, setState] = useState<{ session: StaffSession | null; ready: boolean }>({ session: null, ready: false });
  useEffect(() => {
    const off = onStaffSession((session) => {
      if (!session) client.clear();
      setState({ session, ready: true });
    });
    void refreshStaff().then((session) => setState({ session, ready: true }));
    return () => {
      off();
    };
  }, [client]);
  return state;
}

function Loading() {
  return (
    <div className="flex justify-center py-16">
      <Spinner />
    </div>
  );
}

export default function ConsoleApp() {
  const { session, ready } = useStaff();
  if (!ready) return <Loading />;
  if (!session) return <StaffLogin />;
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/console" element={<Shell session={session} />}>
          <Route index element={<OverviewPage role={session.user.role} />} />
          <Route
            path="clients/new"
            element={STAFF_RANK[session.user.role] >= STAFF_RANK.ops ? <NewClientPage /> : <Navigate to="/console" replace />}
          />
          <Route path="tenants/:id" element={<TenantPage role={session.user.role} />} />
          <Route path="tenants/:id/setup" element={<ClientSetupPage role={session.user.role} />} />
          <Route
            path="staff"
            element={session.user.role === "owner" ? <StaffPage me={session.user.email} /> : <Navigate to="/console" replace />}
          />
          <Route path="reimbursements" element={<LedgerPage role={session.user.role} />} />
        </Route>
        <Route path="*" element={<Navigate to="/console" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

function StaffLogin() {
  const [f, setF] = useState({ email: "", password: "", code: "" });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await staffLogin(f.email.trim(), f.password, f.code.trim());
    } catch (err) {
      setError(err);
      setF((cur) => ({ ...cur, code: "" }));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <Logo className="mb-6 size-12" />
        <h1 className="text-2xl font-semibold tracking-tight">HMH Labz console</h1>
        <p className="mt-1 text-sm text-muted">Staff only. Every tenant, usage, margin and health.</p>
        <form onSubmit={(e) => void onSubmit(e)} className="mt-6 space-y-4">
          <Field label="Email">
            <Input type="email" autoComplete="username" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              autoComplete="current-password"
              required
              value={f.password}
              onChange={(e) => setF({ ...f, password: e.target.value })}
            />
          </Field>
          <Field label="Authenticator code" hint="The 6-digit code from your authenticator app">
            <Input
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              required
              value={f.code}
              onChange={(e) => setF({ ...f, code: e.target.value.replace(/\D/g, "") })}
            />
          </Field>
          <ErrorNote error={error} />
          <Button type="submit" size="lg" className="w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </div>
    </main>
  );
}

const NAV = [
  { to: "/console", label: "Clients", icon: Building2, end: true, owner: false },
  { to: "/console/reimbursements", label: "Reimbursements", icon: Wallet, end: false, owner: false },
  { to: "/console/staff", label: "Staff", icon: ShieldCheck, end: false, owner: true },
];

function Shell({ session }: { session: StaffSession }) {
  const { pathname } = useLocation();
  const nav = NAV.filter((n) => !n.owner || session.user.role === "owner");
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b border-line bg-page/95 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-3 md:px-8">
          <div className="flex min-w-0 items-center gap-4">
            <p className="flex items-center gap-2 text-sm font-semibold">
              <Logo className="size-6 shrink-0" />
              <span className="hidden sm:inline">HMH Labz console</span>
            </p>
            <nav className="flex gap-1" aria-label="Console">
              {nav.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors",
                      isActive ? "bg-line/60 font-medium text-ink [&>svg]:text-accent-ink" : "text-ink-2 hover:bg-line/50 hover:text-ink",
                    )
                  }
                >
                  <Icon className="size-4" aria-hidden />
                  <span>{label}</span>
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm text-muted">
            <span className="hidden sm:inline">
              {session.user.email} · {session.user.role}
            </span>
            <button onClick={() => void staffLogout()} className="flex items-center gap-1.5 hover:text-ink" aria-label="Sign out">
              <LogOut className="size-4" aria-hidden /> <span className="sm:hidden">Sign out</span>
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl px-4 py-4 md:px-8 md:py-8">
        {/* keyed by route: each screen rises into place instead of snapping */}
        <div key={pathname} className="enter">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

function thisMonth(): string {
  return new Date().toISOString().slice(0, 7);
}

function recentMonths(n = 12): string[] {
  const out: string[] = [];
  const d = new Date();
  d.setUTCDate(15);
  for (let i = 0; i < n; i++) {
    out.push(d.toISOString().slice(0, 7));
    d.setUTCMonth(d.getUTCMonth() - 1);
  }
  return out;
}

function MonthPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Select aria-label="Month" className="h-9 w-auto" value={value} onChange={(e) => onChange(e.target.value)}>
      {recentMonths().map((m) => (
        <option key={m} value={m}>
          {m}
        </option>
      ))}
    </Select>
  );
}

function Yes({ ok, label }: { ok: boolean | null; label: string }) {
  return <StatusDot status={ok === null ? "degraded" : ok ? "ok" : "down"} label={label} />;
}

function OverviewPage({ role }: { role: StaffRole }) {
  const [month, setMonth] = useState(thisMonth());
  const q = useQuery({
    queryKey: ["console", "overview", month],
    queryFn: () => consoleApi<Overview>("/overview", { query: { month } }),
    refetchInterval: 60_000,
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Clients">
        <MonthPicker value={month} onChange={setMonth} />
        {STAFF_RANK[role] >= STAFF_RANK.ops ? (
          <Button asChild>
            <Link to="/console/clients/new">
              <Plus aria-hidden /> New client
            </Link>
          </Button>
        ) : null}
      </PageTitle>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : (
        <>
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <Yes ok={q.data.platform.database} label="Database" />
            <Yes ok={q.data.platform.redis} label="Redis" />
            <Yes ok={q.data.platform.worker} label="Worker" />
            <span className="text-sm text-muted">Queue {q.data.platform.queue_depth ?? "—"}</span>
          </div>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Tile label="Revenue" value={aed(q.data.revenue_aed)} sub="Fees for days past each free period" />
            <Tile label="Direct cost" value={aed(q.data.direct_cost_aed)} sub="LLM plus Meta charges HMH Labz bears" />
            <Tile label="LLM" value={aed(q.data.totals.llm_cost_aed)} sub={`USD ${q.data.totals.llm_cost_usd}`} />
            <Tile
              label="Messages"
              value={String(q.data.totals.msgs_in + q.data.totals.msgs_out)}
              sub={`${q.data.totals.msgs_in} in · ${q.data.totals.msgs_out} out`}
            />
          </div>
          {q.data.tenants.length === 0 ? (
            <Card>
              <Empty title="No clients yet">
                {STAFF_RANK[role] >= STAFF_RANK.ops ? (
                  <Link to="/console/clients/new" className="font-medium text-accent-ink hover:underline">
                    Add the first client
                  </Link>
                ) : (
                  "An ops or owner account adds clients."
                )}
              </Empty>
            </Card>
          ) : (
            <div className="enter-list grid gap-3 lg:grid-cols-2">
              {q.data.tenants.map((t) => (
                <TenantCard key={t.id} t={t} month={month} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function TenantCard({ t, month }: { t: TenantRow; month: string }) {
  return (
    <Link to={`/console/tenants/${t.id}?month=${month}`} className="block min-w-0 rounded-xl">
      <Card className="h-full p-4 transition-[border-color,transform,box-shadow] duration-200 hover:-translate-y-px hover:border-ink-2/30 hover:shadow-[0_8px_24px_-16px_rgb(0_0_0/0.25)]">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate font-medium">{t.name}</p>
            <p className="truncate text-xs text-muted">
              {t.slug} · {t.status} · {t.modules.join(", ") || "no modules"}
            </p>
          </div>
          <StatusDot status={t.health.status} />
        </div>
        <dl className="mt-3 grid grid-cols-3 gap-2 text-sm">
          <div>
            <dt className="text-xs text-muted">Meta</dt>
            <dd className="tabular-nums">{aed(t.usage.meta_cost_aed)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">LLM</dt>
            <dd className="tabular-nums">{aed(t.usage.llm_cost_aed)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Margin</dt>
            <dd className="tabular-nums">
              {t.margin.margin_aed === null ? "—" : aed(t.margin.margin_aed)}
              {t.margin.margin_pct !== null ? <span className="text-xs text-muted"> {t.margin.margin_pct}%</span> : null}
            </dd>
          </div>
        </dl>
        {t.health.problems.length ? <p className="mt-2 text-xs text-warn">{t.health.problems.join(" · ")}</p> : null}
        <p className="mt-2 text-xs text-muted">
          Last message in {t.health.last_inbound_at ? ago(t.health.last_inbound_at) : "never"}
          {t.borne_until ? ` · Meta borne until ${dateLabel(t.borne_until)}` : ""}
        </p>
      </Card>
    </Link>
  );
}

function TenantPage({ role }: { role: StaffRole }) {
  const { id = "" } = useParams();
  const [month, setMonth] = useState(new URLSearchParams(window.location.search).get("month") ?? thisMonth());
  const client = useQueryClient();
  const key = ["console", "tenant", id, month];
  const q = useQuery({ queryKey: key, queryFn: () => consoleApi<TenantDetail>(`/tenants/${id}`, { query: { month } }) });
  const pull = useMutation({
    mutationFn: () => consoleApi<TenantDetail>(`/tenants/${id}/statement/pull`, { method: "POST", query: { month } }),
    onSuccess: (data) => client.setQueryData(key, data),
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorNote error={q.error} />;
  const { tenant: t, days, statement, ledger } = q.data;
  return (
    <div className="space-y-4">
      <PageTitle title={t.name}>
        <MonthPicker value={month} onChange={setMonth} />
      </PageTitle>
      <ClientTabs id={id} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile
          label="Meta charges"
          value={aed(t.usage.meta_cost_aed)}
          sub={Number(t.usage.borne_aed) ? `${aed(t.usage.borne_aed)} borne by HMH Labz` : "Paid by the client"}
        />
        <Tile
          label="LLM"
          value={aed(t.usage.llm_cost_aed)}
          sub={`USD ${t.usage.llm_cost_usd} · ${(t.usage.llm_prompt_tokens + t.usage.llm_completion_tokens).toLocaleString()} tokens`}
        />
        <Tile
          label="Revenue"
          value={aed(t.margin.revenue_aed)}
          sub={t.margin.fee_aed ? `Fee ${aed(t.margin.fee_aed)} a month` : "No fee recorded"}
        />
        <Tile
          label="Margin"
          value={t.margin.margin_aed === null ? "—" : aed(t.margin.margin_aed)}
          sub={
            t.margin.margin_pct !== null
              ? `${t.margin.margin_pct}% after direct costs`
              : t.margin.fee_aed
                ? "Free period: no fee this month, costs only"
                : "No fee recorded"
          }
        />
      </div>

      <Card>
        <CardHeader title="Health" action={<StatusDot status={t.health.status} />} />
        <div className="px-4 pb-4 pt-2 text-sm">
          {t.health.problems.length ? (
            <ul className="list-disc space-y-0.5 pl-5 text-warn">
              {t.health.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : (
            <p className="text-good">Nothing needs attention.</p>
          )}
          <ul className="mt-3 space-y-1 text-ink-2">
            {t.channels.map((c, i) => (
              <li key={i}>
                {c.kind === "telegram" ? (
                  <>Telegram bot {c.telegram_username ? `@${c.telegram_username}` : ""}</>
                ) : (
                  <>
                    {c.display_phone ?? "number not set"} · quality {c.quality_rating ?? "unknown"} · tier {c.messaging_limit_tier ?? "unknown"}
                    {c.token_expires_at ? ` · token until ${dateLabel(c.token_expires_at)}` : ""}
                  </>
                )}
                {c.is_active ? "" : " · inactive"}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted">
            {t.health.awaiting_human} waiting for a person · {t.health.stuck} stuck · {t.health.failed_24h} failed sends in 24 h
            {t.cap_aed ? ` · ${t.pct_of_cap ?? 0}% of the ${aed(t.cap_aed)} cap` : ""}
          </p>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Meta's statement"
          subtitle="Reconciled against usage_daily"
          action={
            STAFF_RANK[role] >= STAFF_RANK.ops ? (
              <Button size="sm" variant="secondary" disabled={pull.isPending} onClick={() => pull.mutate()}>
                {pull.isPending ? "Pulling…" : "Pull from Meta"}
              </Button>
            ) : null
          }
        />
        <div className="px-4">
          <ErrorNote error={pull.error} />
        </div>
        <StatementView statement={statement} />
      </Card>

      {ledger.length ? (
        <Card>
          <CardHeader title="Reimbursement ledger" subtitle="Meta charges HMH Labz pays back, by service month" />
          <LedgerTable rows={ledger} role={role} showTenant={false} />
        </Card>
      ) : null}

      <Card>
        <CardHeader title="By day" subtitle="UTC days, as metered" />
        {days.length === 0 ? (
          <Empty title="No usage this month" />
        ) : (
          <div className="overflow-x-auto px-4 pb-4 pt-2">
            <table className="w-full min-w-[30rem] text-sm">
              <thead>
                <tr className="text-xs text-muted">
                  <th className="pb-1 text-left font-normal">Day</th>
                  <th className="pb-1 text-right font-normal">In</th>
                  <th className="pb-1 text-right font-normal">Out</th>
                  <th className="pb-1 text-right font-normal">Marketing</th>
                  <th className="pb-1 text-right font-normal">Meta</th>
                  <th className="pb-1 text-right font-normal">LLM USD</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {days.map((d) => (
                  <tr key={d.day} className="border-t border-line">
                    <td className="py-1.5">{dateLabel(d.day)}</td>
                    <td className="py-1.5 text-right">{d.msgs_in}</td>
                    <td className="py-1.5 text-right">{d.msgs_out}</td>
                    <td className="py-1.5 text-right">{d.counts.marketing ?? 0}</td>
                    <td className="py-1.5 text-right">{aed(d.meta_cost_aed)}</td>
                    <td className="py-1.5 text-right">{d.llm_cost_usd}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

const STATE: Record<LedgerRow["state"], { label: string; tone: "neutral" | "warn" | "good" | "accent" }> = {
  upcoming: { label: "Upcoming", tone: "neutral" },
  running: { label: "In progress", tone: "accent" },
  due: { label: "Due", tone: "warn" },
  paid: { label: "Paid", tone: "good" },
};

function LedgerTable({ rows, role, showTenant }: { rows: LedgerRow[]; role: StaffRole; showTenant: boolean }) {
  const client = useQueryClient();
  const pay = useMutation({
    mutationFn: (r: { tenant_id: string; service_month: number; reference: string | null }) =>
      consoleApi<LedgerRow>("/reimbursements", { method: "POST", body: r }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["console"] }),
  });
  return (
    <>
      <div className="px-4">
        <ErrorNote error={pay.error} />
      </div>
      <ul className="mt-2 divide-y divide-line border-t border-line">
        {rows.map((r) => (
          <li key={`${r.tenant_id}-${r.service_month}`} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm">
            <div className="min-w-0">
              <p className="font-medium">
                {showTenant ? `${r.tenant_name} · ` : ""}Month {r.service_month}{" "}
                <span className="font-normal text-muted">
                  {dateLabel(r.start)} – {dateLabel(r.end)}
                </span>
              </p>
              <p className="text-xs text-muted">
                {r.basis === "meta_statement" ? "Meta's statement" : "Our meter (estimate until Meta's month closes)"} · metered{" "}
                {aed(r.metered_aed)}
                {r.statement_aed ? ` · Meta ${aed(r.statement_aed)}` : ""}
                {r.reference ? ` · ref ${r.reference}` : ""}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <span className="tabular-nums font-medium">{aed(r.paid_aed ?? r.payable_aed)}</span>
              <Badge tone={STATE[r.state].tone}>{STATE[r.state].label}</Badge>
              {r.state === "due" && role === "owner" ? (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={pay.isPending}
                  onClick={() => {
                    const reference = window.prompt(`Record ${aed(r.payable_aed)} paid to ${r.tenant_name}. Payment reference (optional):`);
                    if (reference !== null)
                      pay.mutate({ tenant_id: r.tenant_id, service_month: r.service_month, reference: reference || null });
                  }}
                >
                  Mark paid
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function LedgerPage({ role }: { role: StaffRole }) {
  const q = useQuery({
    queryKey: ["console", "ledger"],
    queryFn: () => consoleApi<{ rows: LedgerRow[]; due_aed: string; paid_aed: string }>("/reimbursements"),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Reimbursements" />
      <p className="text-sm text-muted">
        Meta message charges HMH Labz pays back to tenants inside a borne-by-HMH period, by service month. Meta bills each tenant's own
        account; these are the amounts to return.
      </p>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            <Tile label="Due now" value={aed(q.data.due_aed)} emphasis={Number(q.data.due_aed) > 0} />
            <Tile label="Paid so far" value={aed(q.data.paid_aed)} />
          </div>
          <Card>
            {q.data.rows.length === 0 ? (
              <Empty title="No tenant has a borne-by-HMH period" />
            ) : (
              <LedgerTable rows={q.data.rows} role={role} showTenant />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
