import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Card, CardHeader } from "@/components/ui/card";
import { Tile } from "@/components/ui/tile";
import { ErrorNote, PageTitle, Spinner, StatusDot } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { aed, ago, dateLabel } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { Today } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useModules } from "@/modules";

// Charts sit below the fold on a phone: load them after the tiles.
const SpendChart = lazy(() => import("@/components/charts/SpendChart").then((m) => ({ default: m.SpendChart })));

function SpendTile({ spend }: { spend: Today["spend"] }) {
  if (spend.borne_by_hmh) {
    return (
      <Tile
        label="Message spend this month"
        value="Borne by HMH Labz"
        sub={
          <>
            {aed(spend.meta_cost_aed)} of WhatsApp charges covered
            {spend.borne_until ? ` · until ${dateLabel(spend.borne_until)}` : ""}
          </>
        }
      />
    );
  }
  const pct = spend.pct_of_cap ?? 0;
  const tone = pct >= 100 ? "var(--dot-bad)" : pct >= 80 ? "var(--dot-warn)" : "var(--series)";
  return (
    <Card className="h-full p-4">
      <p className="text-sm text-ink-2">Message spend this month</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight md:text-3xl">{aed(spend.meta_cost_aed)}</p>
      {spend.cap_aed ? (
        <>
          <div
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-accent/15"
            role="meter"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.min(pct, 100)}
            aria-label="Share of monthly cap used"
          >
            <div className="h-full rounded-full" style={{ width: `${Math.min(pct, 100)}%`, background: tone }} />
          </div>
          <p className="mt-1 text-xs text-muted">
            {pct}% of the {aed(spend.cap_aed)} cap
          </p>
        </>
      ) : (
        <p className="mt-1 text-xs text-muted">No monthly cap set</p>
      )}
    </Card>
  );
}

export function TodayPage() {
  const modules = useModules();
  const contactLabel = useAuth().session?.tenant.contact_label ?? "Customers";
  const q = useQuery({
    queryKey: ["today"],
    queryFn: () => api<Today>("/today"),
    refetchInterval: usePollInterval(),
  });

  if (q.isPending) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }
  if (q.isError) return <ErrorNote error={q.error} />;
  const t = q.data;

  return (
    <div className={cn("space-y-4 transition-opacity", q.isFetching && "opacity-90")}>
      <PageTitle title="Today">
        <span className="text-sm text-muted">{dateLabel(t.date)}</span>
      </PageTitle>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {modules.map((m) =>
          m.TodayTiles && t.modules[m.key] !== undefined ? <m.TodayTiles key={m.key} data={t.modules[m.key]} today={t} /> : null,
        )}
        <Tile label="Live chats" value={String(t.live_conversations)} sub={`${contactLabel} who wrote in the last 24 h`} to="/conversations" />
        <Tile
          label="Needs a person"
          value={String(t.awaiting_human)}
          sub={t.awaiting_human ? "The agent handed these over" : "Nothing waiting"}
          to="/conversations"
          emphasis={t.awaiting_human > 0}
        />
        <SpendTile spend={t.spend} />
      </div>

      <Card>
        <CardHeader
          title="Agent health"
          subtitle={t.health.last_agent_reply_at ? `Last agent reply ${ago(t.health.last_agent_reply_at)}` : "No agent replies yet"}
          action={<StatusDot status={t.health.status} />}
        />
        <ul className="grid gap-x-6 gap-y-2 px-4 pb-4 pt-3 sm:grid-cols-2">
          {t.health.checks.map((c) => (
            <li key={c.name} className="flex items-start gap-2 text-sm">
              <StatusDot status={c.status} label={c.name.charAt(0).toUpperCase() + c.name.slice(1)} />
              <span className="text-muted">— {c.detail}</span>
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {modules.map((m) =>
          m.TodayChart && t.modules[m.key] !== undefined ? <m.TodayChart key={m.key} data={t.modules[m.key]} /> : null,
        )}
        <Card>
          <CardHeader
            title="Message spend this month"
            subtitle={t.spend.borne_by_hmh ? "Borne by HMH Labz — shown so you can see the cost" : "Cumulative WhatsApp charges against your cap"}
          />
          <Suspense fallback={<div className="h-[180px]" />}>
            <SpendChart spend={t.spend} points={t.spend_by_day} />
          </Suspense>
        </Card>
      </div>
    </div>
  );
}
