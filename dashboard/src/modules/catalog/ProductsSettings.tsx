import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Field, Input, Select } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { aed } from "@/lib/format";
import type { Product } from "@/lib/types";

export default function ProductsSettings() {
  const client = useQueryClient();
  const isAdmin = useCan("admin");
  const products = useQuery({ queryKey: ["products", "all"], queryFn: () => api<Product[]>("/m/catalog/products") });
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  return (
    <Card>
      <CardHeader
        title="Products and prices"
        subtitle="The agent and every new order use these prices"
        action={
          isAdmin ? (
            <Button size="sm" variant="secondary" onClick={() => setEditing("new")}>
              <Plus /> Add
            </Button>
          ) : null
        }
      />
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {(products.data ?? []).map((p) => (
          <li key={p.id} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{p.name_en ?? p.sku}</p>
              <p className="text-xs text-muted">
                {p.sku} · {p.category ?? "—"}
                {p.stock_note ? ` · ${p.stock_note}` : ""}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {!p.is_active ? <Badge>Hidden</Badge> : null}
              <span className="text-sm tabular">{aed(p.price_aed)}</span>
              {isAdmin ? (
                <Button size="sm" variant="ghost" onClick={() => setEditing(p)}>
                  Edit
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {products.isPending ? <div className="p-4"><Spinner /></div> : null}
      {editing ? (
        <ProductSheet
          product={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void client.invalidateQueries({ queryKey: ["products"] });
          }}
        />
      ) : null}
    </Card>
  );
}

function ProductSheet({ product, onClose, onSaved }: { product: Product | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    sku: product?.sku ?? "",
    name_en: product?.name_en ?? "",
    name_ar: product?.name_ar ?? "",
    category: product?.category ?? "water",
    price_aed: product?.price_aed ?? "",
    stock_note: product?.stock_note ?? "",
    is_active: product?.is_active ?? true,
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        name_en: f.name_en,
        name_ar: f.name_ar || null,
        category: f.category,
        price_aed: f.price_aed,
        stock_note: f.stock_note || null,
        is_active: f.is_active,
      };
      return product
        ? api<Product>(`/m/catalog/products/${product.id}`, { method: "PATCH", body })
        : api<Product>("/m/catalog/products", { method: "POST", body: { ...body, sku: f.sku } });
    },
    onSuccess: onSaved,
  });
  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      title={product ? `Edit ${product.name_en ?? product.sku}` : "Add product"}
      footer={
        <Button className="w-full" disabled={save.isPending || !f.name_en || !f.price_aed || (!product && !f.sku)} onClick={() => save.mutate()}>
          Save
        </Button>
      }
    >
      <div className="space-y-4">
        {!product ? (
          <Field label="SKU" hint="Letters, numbers, dot, dash. Cannot be changed later.">
            <Input value={f.sku} onChange={(e) => setF({ ...f, sku: e.target.value })} />
          </Field>
        ) : null}
        <Field label="Name (English)">
          <Input value={f.name_en} onChange={(e) => setF({ ...f, name_en: e.target.value })} />
        </Field>
        <Field label="Name (Arabic)">
          <Input dir="rtl" value={f.name_ar} onChange={(e) => setF({ ...f, name_ar: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Category" hint="Coupon books pay for water only">
            <Select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
              <option value="water">Water</option>
              <option value="snack">Snack</option>
              <option value="other">Other</option>
            </Select>
          </Field>
          <Field label="Price (AED)">
            <Input inputMode="decimal" value={f.price_aed} onChange={(e) => setF({ ...f, price_aed: e.target.value.replace(/[^\d.]/g, "") })} />
          </Field>
        </div>
        <Field label="Stock note" hint="Optional, e.g. 'back on Monday'">
          <Input value={f.stock_note} onChange={(e) => setF({ ...f, stock_note: e.target.value })} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="size-4" checked={f.is_active} onChange={(e) => setF({ ...f, is_active: e.target.checked })} />
          Available to order
        </label>
        <ErrorNote error={save.error} />
      </div>
    </Sheet>
  );
}

