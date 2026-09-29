// Printable van list: open orders for one date, grouped by area, with what to collect.
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Printer } from "lucide-react";
import { Link, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Empty, ErrorNote, Spinner } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { aed, dateLabel, localToday, phone } from "@/lib/format";
import type { DeliveryList } from "@/lib/types";

export default function DeliveryListPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const date = params.get("date") ?? localToday();
  const area = params.get("area") ?? "";
  const list = useQuery({
    queryKey: ["delivery", date, area],
    queryFn: () => api<DeliveryList>("/orders/delivery-list", { query: { date, area } }),
  });
  const areas = useQuery({ queryKey: ["orders", "areas"], queryFn: () => api<string[]>("/orders/areas") });
  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };
  const stops = list.data?.groups.reduce((n, g) => n + g.stops.length, 0) ?? 0;

  return (
    <div className="mx-auto max-w-4xl px-4 py-4 print:max-w-none print:p-0">
      <div className="no-print mb-4 flex flex-wrap items-center gap-2">
        <Button variant="ghost" asChild>
          <Link to="/orders">
            <ArrowLeft /> Orders
          </Link>
        </Button>
        <Input type="date" aria-label="Date" className="w-auto" value={date} onChange={(e) => set("date", e.target.value)} />
        <Select aria-label="Area" className="w-auto" value={area} onChange={(e) => set("area", e.target.value)}>
          <option value="">All areas</option>
          {(areas.data ?? []).map((a) => (
            <option key={a}>{a}</option>
          ))}
        </Select>
        <Button className="ml-auto" onClick={() => window.print()} disabled={!stops}>
          <Printer /> Print
        </Button>
      </div>

      <header className="mb-4 flex items-end justify-between border-b border-line pb-2">
        <div>
          <h1 className="text-xl font-semibold">Deliveries · {dateLabel(date)}</h1>
          <p className="text-sm text-muted">
            {session?.tenant.name}
            {area ? ` · ${area}` : ""} · {stops} stop{stops === 1 ? "" : "s"}
          </p>
        </div>
        {list.data?.unscheduled ? (
          <p className="no-print text-sm text-warn">{list.data.unscheduled} open order(s) have no date yet</p>
        ) : null}
      </header>

      <ErrorNote error={list.error} />
      {list.isPending ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : !stops ? (
        <Empty title="Nothing to deliver on this date" />
      ) : (
        list.data?.groups.map((g) => (
          <section key={g.area} className="mb-6">
            <div className="mb-2 flex items-baseline justify-between">
              <h2 className="text-base font-semibold">{g.area}</h2>
              <p className="text-sm text-ink-2 tabular">
                {g.bottles} items · collect {aed(g.to_collect_aed)}
              </p>
            </div>
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted">
                <tr className="border-b border-line">
                  <th className="w-8 py-1 font-medium print:table-cell">✓</th>
                  <th className="py-1 font-medium">Order</th>
                  <th className="py-1 font-medium">Customer</th>
                  <th className="py-1 font-medium">Items</th>
                  <th className="py-1 text-right font-medium">Collect</th>
                </tr>
              </thead>
              <tbody>
                {g.stops.map((s) => (
                  <tr key={s.order_no} className="print-break border-b border-line align-top">
                    <td className="py-2">
                      <span className="inline-block size-4 rounded border border-ink-2" aria-hidden />
                    </td>
                    <td className="py-2 font-medium">
                      #{s.order_no}
                      {s.delivery_slot ? <div className="text-xs font-normal text-muted">{s.delivery_slot}</div> : null}
                    </td>
                    <td className="py-2">
                      {s.customer_name ?? "—"}
                      <div className="text-xs text-muted">{phone(s.wa_id)}</div>
                      {s.address_note ? <div className="text-xs">{s.address_note}</div> : null}
                      {s.notes ? <div className="text-xs italic text-ink-2">{s.notes}</div> : null}
                    </td>
                    <td className="py-2">
                      {s.items.map((i) => (
                        <div key={i.sku}>
                          {i.qty} × {i.name ?? i.sku}
                          {i.paid_with_coupon ? " (coupon)" : ""}
                        </div>
                      ))}
                    </td>
                    <td className="py-2 text-right tabular">{aed(s.total_aed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))
      )}
    </div>
  );
}
