import { Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "@/lib/api";
import type { Health } from "@/lib/types";
import { cn } from "@/lib/utils";

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-5 animate-spin text-muted", className)} aria-label="Loading" />;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {children ? <div className="mt-1 text-sm text-muted">{children}</div> : null}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof ApiError || error instanceof Error ? error.message : "Something went wrong";
  return (
    <p role="alert" className="rounded-lg border border-bad/30 bg-bad/5 px-3 py-2 text-sm text-bad">
      {msg.charAt(0).toUpperCase() + msg.slice(1)}
    </p>
  );
}

const DOT: Record<Health, string> = { ok: "var(--dot-good)", degraded: "var(--dot-warn)", down: "var(--dot-bad)" };
const WORD: Record<Health, string> = { ok: "Healthy", degraded: "Needs attention", down: "Down" };

/** Status never rides on colour alone: a dot plus a word. */
export function StatusDot({ status, label }: { status: Health; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      <span className="size-2.5 rounded-full" style={{ background: DOT[status] }} aria-hidden />
      <span className={status === "ok" ? "text-good" : status === "degraded" ? "text-warn" : "text-bad"}>
        {label ?? WORD[status]}
      </span>
    </span>
  );
}

export function PageTitle({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      {children ? <div className="flex flex-wrap items-center gap-2">{children}</div> : null}
    </div>
  );
}
