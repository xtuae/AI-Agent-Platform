import { Megaphone } from "lucide-react";
import { lazy } from "react";
import { Tile } from "@/components/ui/tile";
import { dateLabel } from "@/lib/format";
import type { ModuleDef } from "../types";
import { reason } from "./shared";
import type { CampaignsPanel, CampaignsToday } from "./types";

function TodayTiles({ data }: { data: unknown }) {
  const d = data as CampaignsToday;
  const blocked = d.quality_block !== null;
  return (
    <Tile
      label="Campaigns"
      value={d.sending ? `${d.sending} sending` : d.paused.length ? `${d.paused.length} paused` : "None running"}
      sub={
        blocked
          ? reason(d.quality_block)
          : d.paused.length
            ? `${d.paused[0]!.name}: ${reason(d.paused[0]!.reason)}`
            : `WhatsApp quality: ${d.quality_rating ?? "unknown"}`
      }
      to="/campaigns"
      emphasis={blocked || d.paused.length > 0}
    />
  );
}

function ContactPanel({ data }: { data: unknown }) {
  const d = data as CampaignsPanel;
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Campaigns</h3>
      {d.received.length ? (
        <ul className="divide-y divide-line rounded-lg border border-line text-sm">
          {d.received.map((r, i) => (
            <li key={i} className="flex items-center justify-between gap-3 px-3 py-2">
              <span className="min-w-0 truncate">{r.campaign}</span>
              <span className="shrink-0 text-xs text-muted">
                {r.sent_at ? `sent ${dateLabel(r.sent_at)}` : r.skip_reason ? reason(r.skip_reason) : r.status}
                {r.replied_at ? " · replied" : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">No campaign messages.</p>
      )}
    </section>
  );
}

export const campaigns: ModuleDef = {
  key: "campaigns",
  nav: [{ to: "/campaigns", label: "Campaigns", icon: Megaphone }],
  routes: [
    { path: "campaigns", element: lazy(() => import("./CampaignsPage")) },
    { path: "campaigns/templates", element: lazy(() => import("./TemplatesPage")) },
  ],
  TodayTiles,
  ContactPanel,
};
