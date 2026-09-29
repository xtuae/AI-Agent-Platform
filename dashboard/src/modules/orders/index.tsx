import { Package } from "lucide-react";
import { lazy, Suspense } from "react";
import { Link } from "react-router";
import { Card, CardHeader } from "@/components/ui/card";
import { Tile } from "@/components/ui/tile";
import { aed, dateLabel } from "@/lib/format";
import type { ModuleDef } from "../types";
import { StatusBadge } from "./StatusBadge";
import type { OrdersPanel, OrdersToday } from "./types";

const OrdersChart = lazy(() => import("./OrdersChart").then((m) => ({ default: m.OrdersChart })));

function TodayTiles({ data }: { data: unknown }) {
  const d = data as OrdersToday;
  return (
    <Tile
      label="Orders today"
      value={String(d.count)}
      sub={`${aed(d.value_aed)} · ${d.deliveries_due} to deliver today`}
      to="/orders"
    />
  );
}

function TodayChart({ data }: { data: unknown }) {
  const d = data as OrdersToday;
  return (
    <Card>
      <CardHeader title="Orders per day" subtitle="Last 14 days, excluding cancelled" />
      <Suspense fallback={<div className="h-[180px]" />}>
        <OrdersChart data={d.by_day} />
      </Suspense>
      {d.unscheduled > 0 ? (
        <p className="border-t border-line px-4 py-3 text-sm text-ink-2">
          {d.unscheduled} open order{d.unscheduled === 1 ? "" : "s"} still need a delivery date.{" "}
          <Link to="/orders" className="font-medium text-accent-ink underline-offset-2 hover:underline">
            Review
          </Link>
        </p>
      ) : null}
    </Card>
  );
}

function ContactPanel({ data }: { data: unknown }) {
  const d = data as OrdersPanel;
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
        Orders · {d.lifetime_orders} to date{d.last_order_at ? ` · last ${dateLabel(d.last_order_at)}` : ""}
      </h3>
      {d.orders.length ? (
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {d.orders.map((o) => (
            <li key={o.id} className="flex items-center justify-between gap-3 px-3 py-2">
              <span>
                #{o.order_no} <span className="text-xs text-muted">· {dateLabel(o.created_at)}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="tabular">{aed(o.total_aed)}</span>
                <StatusBadge status={o.status} />
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No orders yet.</p>
      )}
    </section>
  );
}

export const orders: ModuleDef = {
  key: "orders",
  nav: [{ to: "/orders", label: "Orders", icon: Package }],
  routes: [
    { path: "orders", element: lazy(() => import("./OrdersPage")) },
    { path: "orders/delivery-list", element: lazy(() => import("./DeliveryListPage")), bare: true },
  ],
  TodayTiles,
  TodayChart,
  ContactPanel,
};
