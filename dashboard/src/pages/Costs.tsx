import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { categoryLabel, StatementView } from "@/components/Statement";
import { Card, CardHeader } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Tile } from "@/components/ui/tile";
import { api } from "@/lib/api";
import { aed, dateLabel } from "@/lib/format";
import type { Costs } from "@/lib/types";
import { cn } from "@/lib/utils";

function monthLabel(ym: string): string {
  const [y = 2000, m = 1] = ym.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 15)).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

export default function CostsPage() {
  const [month, setMonth] = useState<string | undefined>(undefined);
  const q = useQuery({
    queryKey: ["costs", month ?? "current"],
    queryFn: () => api<Costs>("/costs", { query: { month } }),
  });

  return (
    <div className="space-y-4">
      <PageTitle title="Costs">
        {q.data ? (
          <Select aria-label="Month" className="h-9 w-auto" value={q.data.month} onChange={(e) => setMonth(e.target.value)}>
            {q.data.months.map((m) => (
              <option key={m} value={m}>
                {monthLabel(m)}
              </option>
            ))}
          </Select>
        ) : null}
      </PageTitle>
      {q.isPending ? (
        <div className="flex justify-center py-16">
          <Spinner />
        </div>
      ) : q.isError ? (
        <ErrorNote error={q.error} />
      ) : (
        <CostsBody c={q.data} />
      )}
    </div>
  );
}

function CostsBody({ c }: { c: Costs }) {
  // any day of this month inside the borne-by-HMH period, whether or not anything was spent yet
  const borne = c.borne_until !== null && `${c.month}-01` <= c.borne_until;
  const allBorne = borne && Number(c.due_aed) === 0;
  const max = Math.max(0.01, ...c.days.map((d) => Number(d.total_aed)));
  return (
    <>
      <p className="text-sm text-muted">
        WhatsApp message charges from Meta for {monthLabel(c.month)}, by the day they were sent (UTC). Meta bills these to your WhatsApp
        account.
      </p>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <Tile
          label="Message spend"
          value={aed(c.total_aed)}
          sub={c.cap_aed ? `${c.pct_of_cap?.toFixed(1) ?? 0}% of the ${aed(c.cap_aed)} monthly cap` : "No monthly cap set"}
          emphasis={(c.pct_of_cap ?? 0) >= 80}
        />
        {borne ? (
          <Tile
            label="Borne by HMH Labz"
            value={allBorne ? "All of it" : aed(c.borne_aed)}
            sub={c.borne_until ? `Charges up to ${dateLabel(c.borne_until)} are reimbursed to you` : undefined}
          />
        ) : null}
        <Tile label={borne ? "Yours to pay" : "Amount due to Meta"} value={aed(c.due_aed)} sub="Billed by Meta, not HMH Labz" />
      </div>

      <Card>
        <CardHeader title="By category" subtitle="Service replies are free inside the 24-hour window" />
        <ul className="mt-2 divide-y divide-line border-t border-line">
          {c.categories.map((k) => (
            <li key={k.category} className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
              <span className="text-ink-2">
                {categoryLabel(k.category)}{" "}
                <span className="text-muted">
                  · {k.count} message{k.count === 1 ? "" : "s"}
                </span>
              </span>
              <span className="tabular-nums">{aed(k.cost_aed)}</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card>
        <CardHeader title="By day" />
        {c.days.length === 0 ? (
          <Empty title="No messages this month" />
        ) : (
          <ul className="mt-2 divide-y divide-line border-t border-line">
            {c.days.map((d) => (
              <li key={d.day} className="px-4 py-2.5 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ink-2">
                    {dateLabel(d.day)}
                    {d.borne_by_hmh ? <span className="ml-2 text-xs text-muted">borne by HMH Labz</span> : null}
                  </span>
                  <span className="tabular-nums">{aed(d.total_aed)}</span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-accent/10" aria-hidden>
                  <div
                    className={cn("h-full rounded-full bg-[var(--series)]")}
                    style={{ width: `${(Number(d.total_aed) / max) * 100}%` }}
                  />
                </div>
                <p className="mt-1 text-xs text-muted">
                  {d.msgs_out} sent · {d.msgs_in} received
                  {d.counts.marketing ? ` · ${d.counts.marketing} marketing ${aed(d.costs.marketing)}` : ""}
                  {d.counts.utility ? ` · ${d.counts.utility} utility ${aed(d.costs.utility)}` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader title="Meta's statement" subtitle="What Meta itself reports charging, checked against our meter" />
        <StatementView statement={c.statement} />
      </Card>
    </>
  );
}
