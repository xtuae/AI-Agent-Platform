// The dashboard side of the module contract (the API side is api/modules/base.py).
// A module lives in src/modules/<key>/ and exports one ModuleDef from index.ts. The shell never
// imports a module directly: it asks `useModules()` for the modules the signed-in tenant has.
import type { ComponentType, LazyExoticComponent } from "react";
import type { LucideIcon } from "lucide-react";
import type { CustomerDetail, Today } from "@/lib/types";

export interface NavEntry {
  to: string;
  label: string;
  icon: LucideIcon;
}

export interface RouteEntry {
  path: string; // relative to the app root, e.g. "orders"
  element: LazyExoticComponent<ComponentType>;
  /** Full-screen (printable) pages render outside the app chrome. */
  bare?: boolean;
}

export interface ModuleDef {
  key: string; // must match the API module key
  nav?: NavEntry[];
  routes?: RouteEntry[];
  /** Stat tiles on Today, fed by today.modules[key]. */
  TodayTiles?: ComponentType<{ data: unknown; today: Today }>;
  /** A chart card on Today, fed by today.modules[key]. */
  TodayChart?: ComponentType<{ data: unknown }>;
  /** A section in the contact drawer, fed by contact.modules[key]. */
  ContactPanel?: ComponentType<{ data: unknown; contact: CustomerDetail }>;
  /** A card on the Settings screen. */
  SettingsSection?: ComponentType;
}
