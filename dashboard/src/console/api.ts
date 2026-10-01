// Platform console API client: HMH Labz staff sessions, separate from tenant logins. Same rules as
// the tenant client: the access token lives in memory only, the refresh cookie is HttpOnly and
// scoped to /api/v1/platform/auth, one shared refresh on a 401.
import { ApiError, buildUrl, type RequestOptions } from "@/lib/api";

export type StaffRole = "support" | "ops" | "owner";

export interface StaffSession {
  access_token: string;
  expires_at: string;
  user: { id: string; email: string; name: string | null; role: StaffRole };
}

type Listener = (s: StaffSession | null) => void;
let session: StaffSession | null = null;
let refreshing: Promise<StaffSession | null> | null = null;
const listeners = new Set<Listener>();
const CSRF = { "X-HMH-CSRF": "1" };
const AUTH = "/api/v1/platform/auth";

function set(next: StaffSession | null): void {
  session = next;
  for (const l of listeners) l(next);
}

export function onStaffSession(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function refreshStaff(): Promise<StaffSession | null> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const r = await fetch(`${AUTH}/refresh`, { method: "POST", headers: CSRF, credentials: "same-origin" });
        const next = r.ok ? ((await r.json()) as StaffSession) : null;
        set(next);
        return next;
      } catch {
        return session;
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

async function fail(r: Response): Promise<never> {
  const data: unknown = await r.json().catch(() => null);
  const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
  throw new ApiError(r.status, detail);
}

export async function staffLogin(email: string, password: string, code: string): Promise<StaffSession> {
  const r = await fetch(`${AUTH}/login`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, code }),
  });
  if (!r.ok) return fail(r);
  const s = (await r.json()) as StaffSession;
  set(s);
  return s;
}

export async function staffLogout(): Promise<void> {
  try {
    await fetch(`${AUTH}/logout`, { method: "POST", headers: CSRF, credentials: "same-origin" });
  } finally {
    set(null);
  }
}

/** GET/POST under /api/v1/platform. */
export async function consoleApi<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const send = (token: string | undefined) =>
    fetch(buildUrl(`/platform${path}`, opts.query), {
      method: opts.method ?? "GET",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  let r = await send(session?.access_token);
  if (r.status === 401) {
    const next = await refreshStaff();
    if (!next) throw new ApiError(401, "Your session has ended. Please sign in again.");
    r = await send(next.access_token);
  }
  if (!r.ok) return fail(r);
  return (await r.json()) as T;
}

export const STAFF_RANK: Record<StaffRole, number> = { support: 0, ops: 1, owner: 2 };
