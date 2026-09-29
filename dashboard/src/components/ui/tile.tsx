import type { ReactNode } from "react";
import { Link } from "react-router";
import { cn } from "@/lib/utils";
import { Card } from "./card";

/** A stat tile: label, one figure, an optional line of context. Links when `to` is given. */
export function Tile({
  label,
  value,
  sub,
  to,
  emphasis,
}: {
  label: string;
  value: string;
  sub?: ReactNode;
  to?: string;
  emphasis?: boolean;
}) {
  const body = (
    <Card className={cn("h-full p-4", to && "transition-colors hover:border-accent/40", emphasis && "border-warn/50")}>
      <p className="text-sm text-ink-2">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight md:text-3xl">{value}</p>
      {sub ? <div className="mt-1 text-xs text-muted">{sub}</div> : null}
    </Card>
  );
  return to ? (
    <Link to={to} className="block rounded-xl">
      {body}
    </Link>
  ) : (
    body
  );
}
