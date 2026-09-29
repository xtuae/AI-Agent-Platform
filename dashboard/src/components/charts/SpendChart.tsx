// Message spend this month, cumulative, against the monthly cap — one line + a reference line on
// the same AED axis (never a second axis).
import { useState } from "react";
import { aed, dateLabel } from "@/lib/format";
import type { Today } from "@/lib/types";
import { niceTicks, TableView, Tooltip, useWidth } from "./common";

const H = 180;
const PAD = { top: 16, right: 12, bottom: 24, left: 44 };

function daysInMonth(month: string): string[] {
  const [y, m] = month.split("-").map(Number) as [number, number];
  const n = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return Array.from({ length: n }, (_, i) => `${month}-${String(i + 1).padStart(2, "0")}`);
}

export function SpendChart({ spend, points }: { spend: Today["spend"]; points: Today["spend_by_day"] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const days = daysInMonth(spend.month);
  const values = points.map((p) => Number(p.cumulative_aed));
  const cap = spend.cap_aed ? Number(spend.cap_aed) : null;
  const ticks = niceTicks(Math.max(...values, cap ?? 0, 1));
  const top = ticks[ticks.length - 1] ?? 1;
  const plotW = Math.max(width - PAD.left - PAD.right, 0);
  const plotH = H - PAD.top - PAD.bottom;
  const x = (day: string) => PAD.left + (days.indexOf(day) / Math.max(days.length - 1, 1)) * plotW;
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH;
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.day)},${y(values[i] ?? 0)}`).join("");
  const last = points[points.length - 1];

  function onMove(e: React.PointerEvent<SVGRectElement>) {
    if (!points.length) return;
    const px = e.nativeEvent.offsetX;
    let best = 0;
    points.forEach((p, i) => {
      if (Math.abs(x(p.day) - px) < Math.abs(x(points[best]!.day) - px)) best = i;
    });
    setHover(best);
  }

  return (
    <div>
      <div ref={ref} className="relative px-2">
        {width > 0 ? (
          <svg width={width} height={H} className="overflow-visible" role="img" aria-label={`Message spend this month: ${aed(spend.meta_cost_aed)}${cap ? ` of a ${aed(spend.cap_aed)} cap` : ""}`}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} stroke="rgb(var(--line))" strokeWidth={1} shapeRendering="crispEdges" />
                <text x={PAD.left - 6} y={y(t)} dy="0.32em" textAnchor="end" className="tabular" fontSize={11} fill="rgb(var(--muted))">
                  {t.toLocaleString("en")}
                </text>
              </g>
            ))}
            {cap !== null ? (
              <g>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(cap)} y2={y(cap)} stroke="rgb(var(--ink-2))" strokeWidth={1} shapeRendering="crispEdges" />
                <text x={width - PAD.right} y={y(cap) - 5} textAnchor="end" fontSize={11} fill="rgb(var(--ink-2))">
                  Cap {aed(spend.cap_aed)}
                </text>
              </g>
            ) : null}
            {points.length > 1 ? (
              <path d={`${line}V${y(0)}H${x(points[0]!.day)}Z`} fill="var(--series-wash)" />
            ) : null}
            <path d={line} fill="none" stroke="var(--series)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {last ? (
              <circle cx={x(last.day)} cy={y(Number(last.cumulative_aed))} r={4} fill="var(--series)" stroke="rgb(var(--surface))" strokeWidth={2} />
            ) : null}
            {[days[0], days[Math.floor(days.length / 2)], days[days.length - 1]].map((d, i) =>
              d ? (
                <text key={d} x={x(d)} y={H - 6} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"} fontSize={11} fill="rgb(var(--muted))">
                  {dateLabel(d)}
                </text>
              ) : null,
            )}
            <line x1={PAD.left} x2={width - PAD.right} y1={y(0)} y2={y(0)} stroke="rgb(var(--baseline))" strokeWidth={1} shapeRendering="crispEdges" />
            {hover !== null && points[hover] ? (
              <line x1={x(points[hover].day)} x2={x(points[hover].day)} y1={PAD.top} y2={y(0)} stroke="rgb(var(--baseline))" strokeWidth={1} />
            ) : null}
            <rect
              x={PAD.left}
              y={PAD.top}
              width={plotW}
              height={plotH}
              fill="transparent"
              onPointerMove={onMove}
              onPointerLeave={() => setHover(null)}
            />
          </svg>
        ) : (
          <div style={{ height: H }} />
        )}
        {points.length === 0 ? (
          <p className="absolute inset-0 flex items-center justify-center text-sm text-muted">No paid messages yet this month</p>
        ) : null}
        {hover !== null && points[hover] ? (
          <Tooltip x={x(points[hover].day) + 8} y={y(values[hover] ?? 0)} width={width}>
            <p className="font-semibold text-ink">{aed(points[hover].cumulative_aed)}</p>
            <p className="text-muted">by {dateLabel(points[hover].day)}</p>
          </Tooltip>
        ) : null}
      </div>
      <TableView
        caption="Cumulative message spend"
        head={["Day", "Spent so far"]}
        rows={points.map((p) => [dateLabel(p.day), aed(p.cumulative_aed)])}
      />
    </div>
  );
}
