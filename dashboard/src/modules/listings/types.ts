// Response shapes of /api/v1/m/listings (mirrors api/modules/listings/routes.py).

export type ListingStatus = "draft" | "available" | "under_offer" | "let" | "sold" | "archived";
export type Purpose = "sale" | "rent";
export type PropertyType = "apartment" | "villa" | "townhouse" | "office" | "retail" | "land" | "other";

export interface Listing {
  id: string;
  ref: string;
  title: string;
  description: string | null;
  purpose: Purpose;
  property_type: PropertyType;
  area: string | null;
  community: string | null;
  address_note: string | null;
  bedrooms: number | null;
  bathrooms: number | null;
  size_sqft: number | null;
  price_aed: string | null;
  rent_period: "year" | "month" | null;
  status: ListingStatus;
  viewings_enabled: boolean;
  created_at: string;
  updated_at: string;
}

/** today.modules.listings */
export interface ListingsToday {
  available: number;
  under_offer: number;
  draft: number;
}
