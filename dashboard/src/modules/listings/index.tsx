import { Building2 } from "lucide-react";
import { lazy } from "react";
import { Tile } from "@/components/ui/tile";
import type { ModuleDef } from "../types";
import type { ListingsToday } from "./types";

function TodayTiles({ data }: { data: unknown }) {
  const d = data as ListingsToday;
  return (
    <Tile
      label="Properties on the market"
      value={String(d.available)}
      sub={`${d.under_offer} under offer · ${d.draft} draft`}
      to="/listings"
    />
  );
}

export const listings: ModuleDef = {
  key: "listings",
  nav: [{ to: "/listings", label: "Listings", icon: Building2 }],
  routes: [{ path: "listings", element: lazy(() => import("./ListingsPage")) }],
  TodayTiles,
};
