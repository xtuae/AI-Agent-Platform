import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, PageTitle, Spinner } from "@/components/ui/misc";
import { Sheet } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { useCan } from "@/lib/auth";
import { aed, label, STATUS_LABEL } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { Page } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { Listing, ListingStatus, PropertyType, Purpose } from "./types";

const STATUSES: ListingStatus[] = ["draft", "available", "under_offer", "let", "sold", "archived"];
const TYPES: PropertyType[] = ["apartment", "villa", "townhouse", "office", "retail", "land", "other"];
const TONE: Record<ListingStatus, "neutral" | "accent" | "good" | "warn" | "bad"> = {
  draft: "neutral",
  available: "good",
  under_offer: "warn",
  let: "accent",
  sold: "accent",
  archived: "neutral",
};
const PAGE = 50;

export function price(x: Pick<Listing, "price_aed" | "purpose" | "rent_period">): string {
  const p = aed(x.price_aed);
  return x.purpose === "rent" && x.rent_period ? `${p} / ${x.rent_period}` : p;
}

function beds(n: number | null): string {
  return n === null ? "" : n === 0 ? "Studio" : `${n} bed`;
}

export default function ListingsPage() {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<ListingStatus | "">("");
  const [purpose, setPurpose] = useState<Purpose | "">("");
  const [page, setPage] = useState(0);
  const [editing, setEditing] = useState<Listing | "new" | null>(null);
  const canEdit = useCan("agent");
  const query = {
    q: q.trim() || undefined,
    status: status || undefined,
    purpose: purpose || undefined,
    limit: PAGE,
    offset: page * PAGE,
  };
  const list = useQuery({
    queryKey: ["listings", query],
    queryFn: () => api<Page<Listing>>("/m/listings", { query }),
    placeholderData: keepPreviousData,
    refetchInterval: usePollInterval(),
  });

  return (
    <div>
      <PageTitle title="Listings">
        {list.data ? <span className="text-sm text-muted">{list.data.total.toLocaleString("en")} shown</span> : null}
        {canEdit ? (
          <Button onClick={() => setEditing("new")}>
            <Plus /> New listing
          </Button>
        ) : null}
      </PageTitle>
      <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_11rem_9rem]">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            className="pl-9"
            placeholder="Ref, title, area or community"
            aria-label="Search listings"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(0);
            }}
          />
        </div>
        <Select aria-label="Status" value={status} onChange={(e) => (setStatus(e.target.value as ListingStatus | ""), setPage(0))}>
          <option value="">Any status but archived</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {label(STATUS_LABEL, s)}
            </option>
          ))}
        </Select>
        <Select aria-label="For" value={purpose} onChange={(e) => (setPurpose(e.target.value as Purpose | ""), setPage(0))}>
          <option value="">Sale or rent</option>
          <option value="sale">For sale</option>
          <option value="rent">For rent</option>
        </Select>
      </div>
      <ErrorNote error={list.error} />
      <Card className={cn("overflow-hidden transition-opacity", list.isPlaceholderData && list.isFetching && "opacity-60")}>
        {list.isPending ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : !list.data?.items.length ? (
          <Empty title="No listings match">Only listings marked Available are visible to the agent.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {list.data.items.map((x) => (
              <li key={x.id}>
                <button
                  className="grid w-full grid-cols-[1fr_auto] gap-x-3 px-4 py-3 text-left hover:bg-line/30 md:grid-cols-[1.6fr_1fr_auto_auto]"
                  onClick={() => setEditing(x)}
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium">{x.title}</p>
                    <p className="truncate text-xs text-muted">
                      {x.ref} · {[x.community, x.area].filter(Boolean).join(", ") || "No area"}
                    </p>
                  </div>
                  <div className="text-right md:text-left">
                    <Badge tone={TONE[x.status]}>{label(STATUS_LABEL, x.status)}</Badge>
                  </div>
                  <p className="text-sm text-ink-2 md:self-center">
                    {[beds(x.bedrooms), x.property_type, x.purpose === "rent" ? "for rent" : "for sale"].filter(Boolean).join(" · ")}
                  </p>
                  <p className="tabular text-right text-sm md:self-center">{price(x)}</p>
                </button>
              </li>
            ))}
          </ul>
        )}
        {list.data && list.data.total > PAGE ? (
          <div className="flex items-center justify-between border-t border-line px-4 py-2 text-sm text-muted">
            <span>
              {page * PAGE + 1}–{Math.min((page + 1) * PAGE, list.data.total)} of {list.data.total}
            </span>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Previous
              </Button>
              <Button size="sm" variant="secondary" disabled={(page + 1) * PAGE >= list.data.total} onClick={() => setPage(page + 1)}>
                Next
              </Button>
            </div>
          </div>
        ) : null}
      </Card>
      {editing ? <ListingSheet listing={editing === "new" ? null : editing} readOnly={!canEdit} onClose={() => setEditing(null)} /> : null}
    </div>
  );
}

function ListingSheet({ listing, readOnly, onClose }: { listing: Listing | null; readOnly: boolean; onClose: () => void }) {
  const client = useQueryClient();
  const [f, setF] = useState({
    ref: listing?.ref ?? "",
    title: listing?.title ?? "",
    description: listing?.description ?? "",
    purpose: listing?.purpose ?? ("rent" as Purpose),
    property_type: listing?.property_type ?? ("apartment" as PropertyType),
    area: listing?.area ?? "",
    community: listing?.community ?? "",
    address_note: listing?.address_note ?? "",
    bedrooms: listing?.bedrooms?.toString() ?? "",
    bathrooms: listing?.bathrooms?.toString() ?? "",
    size_sqft: listing?.size_sqft?.toString() ?? "",
    price_aed: listing?.price_aed ?? "",
    rent_period: listing?.rent_period ?? "year",
    status: listing?.status ?? ("draft" as ListingStatus),
    viewings_enabled: listing?.viewings_enabled ?? true,
  });
  const int = (v: string) => (v.trim() === "" ? null : Number(v));
  const save = useMutation({
    mutationFn: () => {
      const body = {
        title: f.title.trim(),
        description: f.description.trim() || null,
        purpose: f.purpose,
        property_type: f.property_type,
        area: f.area.trim() || null,
        community: f.community.trim() || null,
        address_note: f.address_note.trim() || null,
        bedrooms: int(f.bedrooms),
        bathrooms: int(f.bathrooms),
        size_sqft: int(f.size_sqft),
        price_aed: f.price_aed.trim() || null,
        rent_period: f.purpose === "rent" ? f.rent_period : null,
        status: f.status,
        viewings_enabled: f.viewings_enabled,
      };
      return listing
        ? api<Listing>(`/m/listings/${listing.id}`, { method: "PATCH", body })
        : api<Listing>("/m/listings", {
            method: "POST",
            body: { ...body, ref: f.ref.trim() },
          });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["listings"] });
      onClose();
    },
  });
  const needsPrice = f.status === "available" && !f.price_aed.trim();
  const digits = (v: string) => v.replace(/[^\d]/g, "");

  return (
    <Sheet
      open
      onOpenChange={(v) => !v && onClose()}
      wide
      title={listing ? `${listing.ref} · ${listing.title}` : "New listing"}
      description={listing ? `${label(STATUS_LABEL, listing.status)} · ${price(listing)}` : undefined}
      footer={
        readOnly ? undefined : (
          <Button
            className="w-full"
            disabled={save.isPending || !f.title.trim() || (!listing && !f.ref.trim()) || needsPrice}
            onClick={() => save.mutate()}
          >
            Save
          </Button>
        )
      }
    >
      <fieldset disabled={readOnly} className="space-y-4">
        {!listing ? (
          <Field label="Reference" hint="Your own ref, e.g. JVC-1204. Customers may quote it.">
            <Input value={f.ref} onChange={(e) => setF({ ...f, ref: e.target.value })} />
          </Field>
        ) : null}
        <Field label="Title">
          <Input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="For">
            <Select value={f.purpose} onChange={(e) => setF({ ...f, purpose: e.target.value as Purpose })}>
              <option value="rent">Rent</option>
              <option value="sale">Sale</option>
            </Select>
          </Field>
          <Field label="Type">
            <Select value={f.property_type} onChange={(e) => setF({ ...f, property_type: e.target.value as PropertyType })}>
              {TYPES.map((t) => (
                <option key={t} value={t}>
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Price (AED)">
            <Input
              inputMode="decimal"
              value={f.price_aed}
              onChange={(e) => setF({ ...f, price_aed: e.target.value.replace(/[^\d.]/g, "") })}
            />
          </Field>
          {f.purpose === "rent" ? (
            <Field label="Per">
              <Select
                value={f.rent_period}
                onChange={(e) =>
                  setF({
                    ...f,
                    rent_period: e.target.value as "year" | "month",
                  })
                }
              >
                <option value="year">Year</option>
                <option value="month">Month</option>
              </Select>
            </Field>
          ) : null}
        </div>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Bedrooms" hint="0 = studio">
            <Input inputMode="numeric" value={f.bedrooms} onChange={(e) => setF({ ...f, bedrooms: digits(e.target.value) })} />
          </Field>
          <Field label="Bathrooms">
            <Input inputMode="numeric" value={f.bathrooms} onChange={(e) => setF({ ...f, bathrooms: digits(e.target.value) })} />
          </Field>
          <Field label="Size (sq ft)">
            <Input inputMode="numeric" value={f.size_sqft} onChange={(e) => setF({ ...f, size_sqft: digits(e.target.value) })} />
          </Field>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Area" hint="What customers search by">
            <Input value={f.area} onChange={(e) => setF({ ...f, area: e.target.value })} />
          </Field>
          <Field label="Community / building">
            <Input value={f.community} onChange={(e) => setF({ ...f, community: e.target.value })} />
          </Field>
        </div>
        <Field label="Address note" hint="Where viewings happen">
          <Input value={f.address_note} onChange={(e) => setF({ ...f, address_note: e.target.value })} />
        </Field>
        <Field label="Description" hint="The agent may say anything written here, and nothing that is not.">
          <Textarea rows={5} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
        </Field>
        <Field label="Status" hint="Only Available listings are visible to the agent.">
          <Select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value as ListingStatus })}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {label(STATUS_LABEL, s)}
              </option>
            ))}
          </Select>
        </Field>
        {needsPrice ? <p className="text-sm text-warn">An available listing needs a price.</p> : null}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="size-4"
            checked={f.viewings_enabled}
            onChange={(e) => setF({ ...f, viewings_enabled: e.target.checked })}
          />
          Viewings can be booked
        </label>
        <ErrorNote error={save.error} />
      </fieldset>
    </Sheet>
  );
}
