import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Minus, Plus, Printer, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ContactPicker } from "@/components/ContactPicker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { aed, dateLabel, dateTime, label, localToday, phone, STATUS_LABEL } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { CustomerDetail, CustomerRow, Order, OrderDetail, OrderStatus, Page, Product } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useHasModule } from "@/modules";
import type { CouponsPanel } from "@/modules/coupons";
import { StatusBadge } from "./StatusBadge";

const FILTERS: { key: string; label: string; status?: OrderStatus[] }[] = [
  { key: "open", label: "Open", status: ["draft", "confirmed", "out_for_delivery"] },
  { key: "confirmed", label: "Confirmed", status: ["confirmed"] },
  { key: "out", label: "Out for delivery", status: ["out_for_delivery"] },
  { key: "delivered", label: "Delivered", status: ["delivered"] },
  { key: "all", label: "All" },
];
const PAGE = 50;

export default function OrdersPage() {
  const [params, setParams] = useSearchParams();
  const filter = params.get("f") ?? "open";
  const area = params.get("area") ?? "";
  const when = params.get("when") ?? "any";
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const canEdit = useCan("agent");
  const poll = usePollInterval();

  const today = localToday();
  const query = {
    status: FILTERS.find((f) => f.key === filter)?.status,
    area: area || undefined,
    q: q.trim() || undefined,
    date_field: when === "any" ? undefined : "delivery",
    from: when === "today" ? today : when === "week" ? today : undefined,
    to: when === "today" ? today : when === "week" ? addDays(today, 6) : undefined,
    limit: PAGE,
    offset: page * PAGE,
  };
  const orders = useQuery({
    queryKey: ["orders", query],
    queryFn: () => api<Page<Order>>("/m/orders", { query }),
    placeholderData: keepPreviousData,
    refetchInterval: poll,
  });
  const areas = useQuery({ queryKey: ["orders", "areas"], queryFn: () => api<string[]>("/m/orders/areas") });

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setPage(0);
  }

  return (
    <div>
      <PageTitle title="Orders">
        <Button variant="secondary" asChild>
          <Link to={`/orders/delivery-list?date=${when === "any" ? today : today}${area ? `&area=${encodeURIComponent(area)}` : ""}`}>
            <Printer /> Delivery list
          </Link>
        </Button>
        {canEdit ? (
          <Button onClick={() => setCreating(true)}>
            <Plus /> New order
          </Button>
        ) : null}
      </PageTitle>

      {/* filters: one row above the content they scope */}
      <div className="mb-3 flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Status">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            role="tab"
            aria-selected={filter === f.key}
            onClick={() => set("f", f.key === "open" ? "" : f.key)}
            className={cn(
              "h-8 shrink-0 rounded-full border px-3 text-sm",
              filter === f.key ? "border-accent bg-accent/10 font-medium text-accent-ink" : "border-line bg-surface text-ink-2",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-[1fr_12rem_12rem]">
        <div className="relative col-span-2 md:col-span-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            placeholder="Order no, name or phone"
            aria-label="Search orders"
            className="pl-9"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <Select aria-label="Area" value={area} onChange={(e) => set("area", e.target.value)}>
          <option value="">All areas</option>
          {(areas.data ?? []).map((a) => (
            <option key={a}>{a}</option>
          ))}
        </Select>
        <Select aria-label="Delivery date" value={when} onChange={(e) => set("when", e.target.value === "any" ? "" : e.target.value)}>
          <option value="any">Any delivery date</option>
          <option value="today">Delivering today</option>
          <option value="week">Delivering in 7 days</option>
        </Select>
      </div>

      <ErrorNote error={orders.error} />
      {orders.isPending ? (
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      ) : orders.data && orders.data.items.length === 0 ? (
        <Card>
          <Empty title="No orders match">Try another filter{canEdit ? ", or add one with New order" : ""}.</Empty>
        </Card>
      ) : (
        <Card className={cn("overflow-hidden transition-opacity", orders.isFetching && orders.isPlaceholderData && "opacity-60")}>
          {/* phones: cards */}
          <ul className="divide-y divide-line md:hidden">
            {orders.data?.items.map((o) => (
              <li key={o.id}>
                <button className="w-full px-4 py-3 text-left" onClick={() => setOpenId(o.id)}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">
                      #{o.order_no} · {o.customer.name ?? phone(o.customer.wa_id)}
                    </span>
                    <StatusBadge status={o.status} />
                  </div>
                  <div className="mt-1 flex justify-between text-sm text-muted">
                    <span className="truncate">
                      {o.area ?? "No area"} · {o.delivery_date ? dateLabel(o.delivery_date) : "Date TBC"}
                    </span>
                    <span className="tabular text-ink">{aed(o.total_aed)}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          {/* desktop: table */}
          <table className="hidden w-full text-sm md:table">
            <thead className="border-b border-line text-left text-xs text-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Order</th>
                <th className="px-4 py-2 font-medium">Customer</th>
                <th className="px-4 py-2 font-medium">Area</th>
                <th className="px-4 py-2 font-medium">Delivery</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {orders.data?.items.map((o) => (
                <tr key={o.id} className="cursor-pointer hover:bg-line/30" onClick={() => setOpenId(o.id)}>
                  <td className="px-4 py-2.5">
                    <button className="font-medium text-accent-ink" onClick={() => setOpenId(o.id)}>
                      #{o.order_no}
                    </button>
                    <div className="text-xs text-muted">{dateTime(o.created_at)}</div>
                  </td>
                  <td className="px-4 py-2.5">
                    {o.customer.name ?? "—"}
                    <div className="text-xs text-muted">{phone(o.customer.wa_id)}</div>
                  </td>
                  <td className="px-4 py-2.5">{o.area ?? "—"}</td>
                  <td className="px-4 py-2.5">
                    {o.delivery_date ? dateLabel(o.delivery_date) : <span className="text-muted">TBC</span>}
                    {o.delivery_slot ? <div className="text-xs text-muted">{o.delivery_slot}</div> : null}
                  </td>
                  <td className="px-4 py-2.5">
                    <StatusBadge status={o.status} />
                  </td>
                  <td className="tabular px-4 py-2.5 text-right">{aed(o.total_aed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {orders.data && orders.data.total > PAGE ? (
            <div className="flex items-center justify-between border-t border-line px-4 py-2 text-sm text-muted">
              <span>
                {page * PAGE + 1}–{Math.min((page + 1) * PAGE, orders.data.total)} of {orders.data.total}
              </span>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>
                  Previous
                </Button>
                <Button size="sm" variant="secondary" disabled={(page + 1) * PAGE >= orders.data.total} onClick={() => setPage(page + 1)}>
                  Next
                </Button>
              </div>
            </div>
          ) : null}
        </Card>
      )}

      <OrderDrawer id={openId} onClose={() => setOpenId(null)} />
      {creating ? <NewOrderSheet onClose={() => setCreating(false)} onCreated={(id) => setOpenId(id)} /> : null}
    </div>
  );
}

function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

const NEXT_LABEL: Record<OrderStatus, string> = {
  draft: "Back to draft",
  confirmed: "Confirm",
  out_for_delivery: "Out for delivery",
  delivered: "Mark delivered",
  cancelled: "Cancel order",
};

function OrderDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const client = useQueryClient();
  const canEdit = useCan("agent");
  const order = useQuery({
    queryKey: ["order", id],
    queryFn: () => api<OrderDetail>(`/m/orders/${id}`),
    enabled: id !== null,
  });
  const [edit, setEdit] = useState<{ delivery_date: string; delivery_slot: string; notes: string } | null>(null);
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api<Order>(`/m/orders/${id}`, { method: "PATCH", body }),
    onSuccess: () => {
      setEdit(null);
      void client.invalidateQueries({ queryKey: ["orders"] });
      void client.invalidateQueries({ queryKey: ["order", id] });
      void client.invalidateQueries({ queryKey: ["today"] });
    },
  });
  const o = order.data;
  const closed = o ? o.status === "delivered" || o.status === "cancelled" : false;

  return (
    <Sheet
      open={id !== null}
      onOpenChange={(v) => {
        if (!v) {
          setEdit(null);
          patch.reset();
          onClose();
        }
      }}
      title={o ? `Order #${o.order_no}` : "Order"}
      description={o ? `${o.customer.name ?? phone(o.customer.wa_id)} · ${dateTime(o.created_at)}` : undefined}
      footer={
        o && canEdit && o.next_statuses.length ? (
          <div className="flex flex-wrap gap-2">
            {o.next_statuses
              .slice()
              .sort((a) => (a === "cancelled" ? 1 : -1))
              .map((s) => (
                <Button
                  key={s}
                  variant={s === "cancelled" ? "danger" : "primary"}
                  className={s === "cancelled" ? "" : "flex-1"}
                  disabled={patch.isPending}
                  onClick={() => {
                    if (s === "cancelled" && !window.confirm(`Cancel order #${o.order_no}?`)) return;
                    patch.mutate({ status: s });
                  }}
                >
                  {NEXT_LABEL[s]}
                </Button>
              ))}
          </div>
        ) : null
      }
    >
      {!o ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <StatusBadge status={o.status} />
            <span className="text-xs text-muted">via {o.source === "agent" ? "WhatsApp agent" : (o.source ?? "—")}</span>
          </div>
          <ErrorNote error={patch.error} />

          <section>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Items</h3>
            <ul className="divide-y divide-line rounded-lg border border-line">
              {o.items.map((it) => (
                <li key={it.sku} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <span>
                    {it.qty} × {it.name ?? it.sku}
                    {it.paid_with_coupon ? (
                      <Badge tone="accent" className="ml-2">
                        coupon
                      </Badge>
                    ) : null}
                  </span>
                  <span className="tabular text-ink-2">{aed(it.line_total_aed)}</span>
                </li>
              ))}
              <li className="flex justify-between px-3 py-2 text-sm font-semibold">
                <span>Total to collect</span>
                <span className="tabular">{aed(o.total_aed)}</span>
              </li>
            </ul>
          </section>

          <section className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <p className="text-xs text-muted">Customer</p>
              <p>{o.customer.name ?? "—"}</p>
              <a className="text-accent-ink" href={`tel:+${o.customer.wa_id}`}>
                {phone(o.customer.wa_id)}
              </a>
            </div>
            <div>
              <p className="text-xs text-muted">Area</p>
              <p>{o.area ?? "—"}</p>
              {o.customer.address_note ? <p className="text-xs text-muted">{o.customer.address_note}</p> : null}
            </div>
          </section>

          {edit ? (
            <section className="space-y-3 rounded-lg border border-line p-3">
              <Field label="Delivery date">
                <Input
                  type="date"
                  min={localToday()}
                  value={edit.delivery_date}
                  onChange={(e) => setEdit({ ...edit, delivery_date: e.target.value })}
                />
              </Field>
              <Field label="Time slot">
                <Input
                  placeholder="e.g. 4–6 pm"
                  value={edit.delivery_slot}
                  onChange={(e) => setEdit({ ...edit, delivery_slot: e.target.value })}
                />
              </Field>
              <Field label="Notes">
                <Textarea value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} />
              </Field>
              <div className="flex gap-2">
                <Button
                  disabled={patch.isPending}
                  onClick={() =>
                    patch.mutate(
                      closed
                        ? { notes: edit.notes || null }
                        : {
                            delivery_date: edit.delivery_date || null,
                            delivery_slot: edit.delivery_slot || null,
                            notes: edit.notes || null,
                          },
                    )
                  }
                >
                  Save
                </Button>
                <Button variant="ghost" onClick={() => setEdit(null)}>
                  Cancel
                </Button>
              </div>
            </section>
          ) : (
            <section className="flex items-start justify-between gap-3 text-sm">
              <div className="space-y-1">
                <p>
                  <span className="text-muted">Delivery: </span>
                  {o.delivery_date ? dateLabel(o.delivery_date) : "to be confirmed"}
                  {o.delivery_slot ? `, ${o.delivery_slot}` : ""}
                </p>
                {o.notes ? <p className="text-ink-2">{o.notes}</p> : null}
              </div>
              {canEdit ? (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    setEdit({ delivery_date: o.delivery_date ?? "", delivery_slot: o.delivery_slot ?? "", notes: o.notes ?? "" })
                  }
                >
                  Edit
                </Button>
              ) : null}
            </section>
          )}

          <section>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">History</h3>
            <ol className="space-y-1.5 text-sm">
              {o.history.map((h, i) => (
                <li key={i} className="flex justify-between gap-3">
                  <span className="text-ink-2">{describe(h)}</span>
                  <span className="shrink-0 text-xs text-muted">{dateTime(h.at)}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      )}
    </Sheet>
  );
}

function describe(h: OrderDetail["history"][number]): string {
  const who = h.actor === "agent" ? "Agent" : "Team";
  if (h.action === "create_order") return `${who} placed the order`;
  const s = h.after?.status;
  if (typeof s === "string") return `${who}: ${label(STATUS_LABEL, s)}`;
  return `${who} updated ${Object.keys(h.after ?? {})
    .join(", ")
    .replace(/_/g, " ")}`;
}

// ---------------------------------------------------------------- new order

function NewOrderSheet({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const client = useQueryClient();
  const [idempotencyKey] = useState(() => crypto.randomUUID()); // a double tap places one order
  const [customer, setCustomer] = useState<CustomerRow | CustomerDetail | null>(null);
  const [qty, setQty] = useState<Record<string, number>>({});
  const [useCoupon, setUseCoupon] = useState(false);
  const [area, setArea] = useState("");
  const [date, setDate] = useState(localToday());
  const [slot, setSlot] = useState("");
  const [notes, setNotes] = useState("");

  const products = useQuery({
    queryKey: ["products", "active"],
    queryFn: () => api<Product[]>("/m/catalog/products", { query: { active: true } }),
  });
  const create = useMutation({
    mutationFn: () =>
      api<Order>("/m/orders", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: {
          customer_id: customer?.id,
          items: Object.entries(qty)
            .filter(([, n]) => n > 0)
            .map(([sku, n]) => ({ sku, qty: n })),
          use_coupon_book: hasCoupons && useCoupon,
          area: area || null,
          delivery_date: date || null,
          delivery_slot: slot || null,
          notes: notes || null,
        },
      }),
    onSuccess: (o) => {
      void client.invalidateQueries({ queryKey: ["orders"] });
      void client.invalidateQueries({ queryKey: ["today"] });
      onClose();
      onCreated(o.id);
    },
  });

  const lines = (products.data ?? []).filter((p) => (qty[p.sku] ?? 0) > 0);
  // A preview only: the server recomputes the total from its own prices when the order is placed.
  const estimate = useMemo(
    () =>
      lines.reduce((sum, p) => {
        const covered = useCoupon && p.category === "water";
        return sum + (covered ? 0 : Number(p.price_aed ?? 0) * (qty[p.sku] ?? 0));
      }, 0),
    [lines, qty, useCoupon],
  );
  // Prepaid balance comes from the coupons module, when this tenant has it.
  const hasCoupons = useHasModule("coupons");
  const detail = useQuery({
    queryKey: ["customer", customer?.id],
    queryFn: () => api<CustomerDetail>(`/contacts/${customer?.id}`),
    enabled: hasCoupons && customer !== null,
  });
  const bottles = (detail.data?.modules.coupons as CouponsPanel | undefined)?.bottles_remaining ?? 0;

  return (
    <Sheet
      open
      wide
      onOpenChange={(v) => !v && onClose()}
      title="New order"
      footer={
        <div className="space-y-2">
          <ErrorNote error={create.error instanceof ApiError ? create.error : create.error} />
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm">
              <p className="font-semibold tabular">{aed(estimate.toFixed(2))}</p>
              <p className="text-xs text-muted">Estimate · final total is calculated on save</p>
            </div>
            <Button size="lg" disabled={!customer || lines.length === 0 || create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? "Placing…" : "Place order"}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-5">
        <ContactPicker
          value={customer}
          onChange={(c) => {
            setCustomer(c);
            setArea(c?.area ?? "");
            setUseCoupon(false);
          }}
        />

        <section>
          <h3 className="mb-2 text-sm font-medium text-ink-2">Items</h3>
          {products.isPending ? <Spinner /> : null}
          <ul className="divide-y divide-line rounded-lg border border-line">
            {sortForOrdering(products.data ?? []).map((p) => {
              const n = qty[p.sku] ?? 0;
              return (
                <li key={p.id} className="flex items-center justify-between gap-3 px-3 py-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm">{p.name_en ?? p.sku}</p>
                    <p className="text-xs text-muted">{aed(p.price_aed)}</p>
                  </div>
                  <div className="flex items-center gap-1">
                    <Button
                      size="icon"
                      variant="secondary"
                      aria-label={`Fewer ${p.name_en}`}
                      disabled={n === 0}
                      onClick={() => setQty({ ...qty, [p.sku]: Math.max(0, n - 1) })}
                    >
                      <Minus />
                    </Button>
                    <input
                      aria-label={`Quantity of ${p.name_en}`}
                      inputMode="numeric"
                      className="h-10 w-12 rounded-lg border border-line bg-surface text-center text-base tabular"
                      value={n}
                      onChange={(e) =>
                        setQty({ ...qty, [p.sku]: Math.min(1000, Math.max(0, Number(e.target.value.replace(/\D/g, "")) || 0)) })
                      }
                    />
                    <Button
                      size="icon"
                      variant="secondary"
                      aria-label={`More ${p.name_en}`}
                      onClick={() => setQty({ ...qty, [p.sku]: n + 1 })}
                    >
                      <Plus />
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>

        {customer && bottles > 0 ? (
          <label className="flex items-center gap-3 rounded-lg border border-line px-3 py-2.5 text-sm">
            <input
              type="checkbox"
              className="size-5 accent-[rgb(var(--accent))]"
              checked={useCoupon}
              onChange={(e) => setUseCoupon(e.target.checked)}
            />
            <span>
              Use coupon book for water <span className="text-muted">({bottles} left)</span>
            </span>
          </label>
        ) : null}

        <div className="grid grid-cols-2 gap-3">
          <Field label="Area" className="col-span-2 sm:col-span-1">
            <Input value={area} onChange={(e) => setArea(e.target.value)} placeholder="e.g. JLT" />
          </Field>
          <Field label="Delivery date" className="col-span-2 sm:col-span-1">
            <Input type="date" min={localToday()} value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
          <Field label="Time slot" className="col-span-2 sm:col-span-1">
            <Input value={slot} onChange={(e) => setSlot(e.target.value)} placeholder="optional" />
          </Field>
          <Field label="Notes" className="col-span-2">
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="optional" />
          </Field>
        </div>
      </div>
    </Sheet>
  );
}

/** Water first (what nearly every order is), then cross-sell items by their priority. */
function sortForOrdering(products: Product[]): Product[] {
  const rank = (p: Product) => (p.category === "water" ? 0 : 1);
  return [...products].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      (a.cross_sell_priority ?? 99) - (b.cross_sell_priority ?? 99) ||
      (a.name_en ?? "").localeCompare(b.name_en ?? ""),
  );
}
