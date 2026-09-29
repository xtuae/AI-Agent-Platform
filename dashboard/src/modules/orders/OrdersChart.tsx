// Orders per day, last 14 days — column chart, one series (so no legend: the title names it).
import { useState } from "react";
import { aed, dateLabel } from "@/lib/format";
import type { OrdersToday } from "./types";
import { niceTicks, TableView, Tooltip, useWidth } from "@/components/charts/common";

const H = 180;
const PAD = { top: 16, right: 8, bottom: 24, left: 32 };

export function OrdersChart({ data }: { data: OrdersToday["by_day"] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...data.map((d) => d.orders), 0);
  const ticks = niceTicks(Math.max(max, 4), 4, true); // counts: whole numbers only
  const top = ticks[ticks.length - 1] ?? 1;
  const plotW = Math.max(width - PAD.left - PAD.right, 0);
  const plotH = H - PAD.top - PAD.bottom;
  const band = data.length ? plotW / data.length : 0;
  const barW = Math.min(24, Math.max(band - 2, 2)); // <= 24px, 2px surface gap minimum
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH;
  const r = Math.min(4, barW / 2);

  return (
    <div>
      <div ref={ref} className="relative px-2" onMouseLeave={() => setHover(null)}>
        {width > 0 ? (
          <svg width={width} height={H} className="overflow-visible" role="img" aria-label="Orders per day, last 14 days">
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} stroke="rgb(var(--line))" strokeWidth={1} shapeRendering="crispEdges" />
                <text x={PAD.left - 6} y={y(t)} dy="0.32em" textAnchor="end" className="tabular" fontSize={11} fill="rgb(var(--muted))">
                  {t}
                </text>
              </g>
            ))}
            {data.map((d, i) => {
              const cx = PAD.left + band * i + band / 2;
              const h = Math.max(y(0) - y(d.orders), d.orders > 0 ? 2 : 0);
              const x0 = cx - barW / 2;
              const yTop = y(0) - h;
              const rr = Math.min(r, h);
              // 4px rounded data-end, square at the baseline
              const path = h
                ? `M${x0},${y(0)}V${yTop + rr}Q${x0},${yTop} ${x0 + rr},${yTop}H${x0 + barW - rr}Q${x0 + barW},${yTop} ${x0 + barW},${yTop + rr}V${y(0)}Z`
                : "";
              const isLast = i === data.length - 1;
              return (
                <g key={d.day}>
                  {path ? <path d={path} fill="var(--series)" opacity={hover === null || hover === i ? 1 : 0.55} /> : null}
                  {/* hit target: the whole band, taller than the mark */}
                  <rect
                    x={PAD.left + band * i}
                    y={PAD.top}
                    width={band}
                    height={plotH}
                    fill="transparent"
                    tabIndex={0}
                    aria-label={`${dateLabel(d.day)}: ${d.orders} orders, ${aed(d.value_aed)}`}
                    onMouseEnter={() => setHover(i)}
                    onFocus={() => setHover(i)}
                    onBlur={() => setHover(null)}
                  />
                  {/* sparse date labels, counted back from Today so they never collide with it */}
                  {(data.length - 1 - i) % Math.max(Math.ceil(data.length / Math.max(Math.floor(plotW / 64), 2)), 2) === 0 && (
                    <text x={cx} y={H - 6} textAnchor="middle" fontSize={11} fill="rgb(var(--muted))">
                      {isLast ? "Today" : dateLabel(d.day)}
                    </text>
                  )}
                  {isLast && d.orders > 0 ? (
                    <text x={cx} y={yTop - 5} textAnchor="middle" fontSize={11} fontWeight={600} fill="rgb(var(--ink))">
                      {d.orders}
                    </text>
                  ) : null}
                </g>
              );
            })}
            <line x1={PAD.left} x2={width - PAD.right} y1={y(0)} y2={y(0)} stroke="rgb(var(--baseline))" strokeWidth={1} shapeRendering="crispEdges" />
          </svg>
        ) : (
          <div style={{ height: H }} />
        )}
        {hover !== null && data[hover] ? (
          <Tooltip x={PAD.left + band * hover + band / 2 + 8} y={y(data[hover].orders)} width={width}>
            <p className="font-semibold text-ink">{data[hover].orders} orders</p>
            <p className="text-muted">
              {dateLabel(data[hover].day)} · {aed(data[hover].value_aed)}
            </p>
          </Tooltip>
        ) : null}
      </div>
      <TableView
        caption="Orders per day"
        head={["Day", "Orders"]}
        rows={data.map((d) => [dateLabel(d.day), `${d.orders} · ${aed(d.value_aed)}`])}
      />
    </div>
  );
}
