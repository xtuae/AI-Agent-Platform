// Every module the dashboard knows, in the API's canonical order. Adding a module = adding its
// folder and one line here; the shell picks up its menu entries, routes, Today tiles, contact
// panel and settings section for the tenants that have it switched on.
import { useMemo } from "react";
import { useAuth } from "@/lib/auth";
import { appointments } from "./appointments";
import { catalog } from "./catalog";
import { coupons } from "./coupons";
import { listings } from "./listings";
import { orders } from "./orders";
import type { ModuleDef } from "./types";

export const MODULES: readonly ModuleDef[] = [catalog, orders, coupons, appointments, listings];

/** The modules the signed-in tenant has, in canonical order. */
export function useModules(): ModuleDef[] {
  const keys = useAuth().session?.tenant.modules;
  return useMemo(() => MODULES.filter((m) => keys?.includes(m.key)), [keys]);
}

export function useHasModule(key: string): boolean {
  return useAuth().session?.tenant.modules.includes(key) ?? false;
}

export type { ModuleDef } from "./types";
