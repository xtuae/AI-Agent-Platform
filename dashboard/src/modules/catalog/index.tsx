import { lazy } from "react";
import type { ModuleDef } from "../types";

export const catalog: ModuleDef = {
  key: "catalog",
  SettingsSection: lazy(() => import("./ProductsSettings")),
};
