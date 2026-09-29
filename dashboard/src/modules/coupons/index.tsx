import { Badge } from "@/components/ui/badge";
import { aed, dateLabel } from "@/lib/format";
import type { ModuleDef } from "../types";

/** contact.modules.coupons */
export interface CouponsPanel {
  bottles_remaining: number;
  books: {
    id: string;
    sku: string | null;
    bottles_total: number | null;
    bottles_free: number | null;
    bottles_remaining: number | null;
    price_aed: string | null;
    purchased_at: string | null;
    expires_at: string | null;
    live: boolean;
  }[];
}

function ContactPanel({ data }: { data: unknown }) {
  const d = data as CouponsPanel;
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
        Coupon books · {d.bottles_remaining} left
      </h3>
      {d.books.length ? (
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {d.books.map((b) => (
            <li key={b.id} className="flex items-center justify-between gap-3 px-3 py-2">
              <div>
                <p>
                  {b.bottles_remaining ?? 0} of {b.bottles_total ?? "?"} left
                  {b.bottles_free ? <span className="text-muted"> ({b.bottles_free} free)</span> : null}
                </p>
                <p className="text-xs text-muted">
                  {b.purchased_at ? `Bought ${dateLabel(b.purchased_at)}` : ""}
                  {b.expires_at ? ` · expires ${dateLabel(b.expires_at)}` : ""}
                  {b.price_aed ? ` · ${aed(b.price_aed)}` : ""}
                </p>
              </div>
              <Badge tone={b.live ? "good" : "neutral"}>{b.live ? "Active" : "Used / expired"}</Badge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No coupon books.</p>
      )}
    </section>
  );
}

export const coupons: ModuleDef = { key: "coupons", ContactPanel };
