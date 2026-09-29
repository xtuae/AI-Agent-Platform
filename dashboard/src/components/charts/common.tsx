import { useEffect, useRef, useState, type ReactNode } from "react";

/** Track an element's width so the SVG draws at real pixel size (crisp hairlines, no scaling). */
export function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const cs = getComputedStyle(el); // content box, like ResizeObserver's contentRect
    setWidth(Math.floor(el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)));
    const ro = new ResizeObserver(([entry]) => entry && setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Clean axis ticks: 0 and 3-4 round steps (1, 2, 2.5, 5 × 10^n) covering max. */
export function niceTicks(max: number, count = 4, integer = false): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const steps = (integer ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10]).map((m) => m * mag);
  const step = Math.max(steps.find((s) => s >= raw) ?? raw, integer ? 1 : 0);
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(6)));
  if ((ticks[ticks.length - 1] ?? 0) < max) ticks.push(Number((ticks.length * step).toFixed(6)));
  return ticks;
}

export function Tooltip({ x, y, children, width }: { x: number; y: number; width: number; children: ReactNode }) {
  // keep inside the plot horizontally
  const left = Math.min(Math.max(x, 60), Math.max(width - 60, 60));
  return (
    <div
      role="status"
      className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs shadow-md"
      style={{ left, top: y - 8 }}
    >
      {children}
    </div>
  );
}

/** Every value a chart shows is also reachable without hovering. */
export function TableView({ caption, head, rows }: { caption: string; head: [string, string]; rows: [string, string][] }) {
  return (
    <details className="mt-2 px-4 pb-3 text-sm">
      <summary className="cursor-pointer text-xs text-muted hover:text-ink-2">Show as table</summary>
      <table className="mt-2 w-full tabular">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-left text-xs text-muted">
            <th className="py-1 font-medium">{head[0]}</th>
            <th className="py-1 text-right font-medium">{head[1]}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([a, b]) => (
            <tr key={a} className="border-t border-line">
              <td className="py-1 text-ink-2">{a}</td>
              <td className="py-1 text-right">{b}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
