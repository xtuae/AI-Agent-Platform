import { Badge } from "@/components/ui/badge";
import { aed, dateLabel, dateTime } from "@/lib/format";
import type { ReconLine, ReconStatus, Statement } from "@/lib/types";

const CATEGORY: Record<string, string> = {
  marketing: "Marketing",
  utility: "Utility",
  service: "Service",
  authentication: "Authentication",
};

export function categoryLabel(key: string): string {
  return CATEGORY[key] ?? key.replace(/_/g, " ");
}

export function ReconBadge({ status }: { status: ReconStatus }) {
  if (status === "ok") return <Badge tone="good">Matches</Badge>;
  if (status === "check") return <Badge tone="warn">Differs</Badge>;
  if (status === "not_comparable") return <Badge>Other currency</Badge>;
  return <Badge>Not pulled</Badge>;
}

function Row({ line, label }: { line: ReconLine; label: string }) {
  return (
    <tr className="border-t border-line">
      <th scope="row" className="py-2 pr-3 text-left font-normal text-ink-2">
        {label}
      </th>
      <td className="py-2 pr-3 text-right tabular-nums">
        {line.ours_count} · {aed(line.ours_aed)}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">
        {line.meta_count ?? "—"} · {aed(line.meta_aed)}
      </td>
      <td className="py-2 text-right">
        <ReconBadge status={line.status} />
      </td>
    </tr>
  );
}

/** Our meter next to Meta's own figures for one month. Shared by Costs and the console. */
export function StatementView({ statement }: { statement: Statement }) {
  const s = statement;
  if (!s.pulled_at || !s.total) {
    return (
      <p className="px-4 pb-4 pt-2 text-sm text-muted">
        Meta's figures for this month have not been pulled yet. They are read from Meta every morning.
      </p>
    );
  }
  const differs = s.days.filter((d) => d.status === "check");
  return (
    <div className="px-4 pb-4 pt-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <ReconBadge status={s.total.status} />
        <span className="text-ink-2">
          {s.total.status === "ok"
            ? "Our meter agrees with Meta within 1%."
            : s.total.status === "check"
              ? "Our meter and Meta differ by more than 1%."
              : "Meta bills this account in another currency."}
        </span>
      </div>
      <p className="mt-1 text-xs text-muted">
        Read from Meta {dateTime(s.pulled_at)}
        {s.complete ? " · month closed" : " · month still open, figures can still change"}
        {s.currency && s.currency !== "AED" && s.meta_total_native ? ` · billed in ${s.currency} (${s.meta_total_native})` : ""}
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[22rem] text-sm">
          <thead>
            <tr className="text-xs text-muted">
              <th className="pb-1 text-left font-normal">Category</th>
              <th className="pb-1 pr-3 text-right font-normal">Our meter</th>
              <th className="pb-1 pr-3 text-right font-normal">Meta</th>
              <th className="pb-1 text-right font-normal">
                <span className="sr-only">Status</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {s.categories.map((c) => (
              <Row key={c.key} line={c} label={categoryLabel(c.key)} />
            ))}
            <Row line={s.total} label="Total" />
          </tbody>
        </table>
      </div>
      {differs.length ? (
        <p className="mt-3 text-xs text-muted">
          Days that differ: {differs.map((d) => `${dateLabel(d.key)} (${aed(d.diff_aed)})`).join(", ")}. Usually a message Meta did not
          deliver, or a price change.
        </p>
      ) : null}
    </div>
  );
}
