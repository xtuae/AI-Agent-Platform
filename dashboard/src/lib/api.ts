// API client. The access token lives in memory only (never localStorage): a page reload restores
// the session through the HttpOnly refresh cookie. Every 401 triggers ONE shared refresh; callers
// waiting on it retry once with the new token.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(ApiError.describe(status, detail));
  }

  get code(): string | undefined {
    const d = this.detail as { code?: unknown } | null;
    return d && typeof d === "object" && typeof d.code === "string" ? d.code : undefined;
  }

  static describe(status: number, detail: unknown): string {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI validation errors
      const first = detail[0] as { msg?: string; loc?: unknown[] } | undefined;
      if (first?.msg) return first.msg.replace(/^Value error, /, "");
    }
    if (detail && typeof detail === "object" && "code" in detail) {
      return String((detail as { code: unknown }).code).replace(/_/g, " ");
    }
    return `Request failed (${status})`;
  }
}

export interface Session {
  access_token: string;
  expires_at: string;
  user: { id: string; email: string; name: string | null; role: Role };
  tenant: {
    id: string;
    name: string;
    slug: string;
    timezone: string;
    meta_charges_borne_by_us_until: string | null;
    /** Enabled module keys: the dashboard shows only what these modules provide. */
    modules: string[];
    contact_label: string;
  };
}
export type Role = "viewer" | "agent" | "admin";

type Listener = (session: Session | null) => void;

let session: Session | null = null;
let refreshing: Promise<Session | null> | null = null;
const listeners = new Set<Listener>();

export function currentSession(): Session | null {
  return session;
}

export function setSession(next: Session | null): void {
  session = next;
  for (const l of listeners) l(next);
}

export function onSessionChange(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

const CSRF = { "X-HMH-CSRF": "1" };

/** Exchange the refresh cookie for a new session. Concurrent callers share one request. */
export function refreshSession(fetchImpl: typeof fetch = fetch): Promise<Session | null> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const r = await fetchImpl("/api/v1/auth/refresh", {
          method: "POST",
          headers: CSRF,
          credentials: "same-origin",
        });
        const next = r.ok ? ((await r.json()) as Session) : null;
        setSession(next);
        return next;
      } catch {
        return session; // offline: keep what we have, the next call will try again
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | string[] | null | undefined>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => params.append(k, x));
    else params.append(k, String(v));
  }
  const qs = params.toString();
  return `/api/v1${path}${qs ? `?${qs}` : ""}`;
}

export async function api<T>(
  path: string,
  opts: RequestOptions = {},
  fetchImpl: typeof fetch = fetch,
): Promise<T> {
  const send = (token: string | undefined) =>
    fetchImpl(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      credentials: "same-origin",
      signal: opts.signal,
      headers: {
        Accept: "application/json",
        ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...opts.headers,
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });

  let r = await send(session?.access_token);
  if (r.status === 401) {
    const next = await refreshSession(fetchImpl);
    if (!next) throw new ApiError(401, "Your session has ended. Please sign in again.");
    r = await send(next.access_token);
  }
  if (r.status === 204) return undefined as T;
  const data: unknown = await r.json().catch(() => null);
  if (!r.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(r.status, detail);
  }
  return data as T;
}

export async function login(
  email: string,
  password: string,
  tenant?: string,
): Promise<Session> {
  const r = await fetch("/api/v1/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tenant ? { email, password, tenant } : { email, password }),
  });
  const data: unknown = await r.json().catch(() => null);
  if (!r.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    throw new ApiError(r.status, detail);
  }
  setSession(data as Session);
  return data as Session;
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/v1/auth/logout", { method: "POST", headers: CSRF, credentials: "same-origin" });
  } finally {
    setSession(null);
  }
}

export const ROLE_RANK: Record<Role, number> = { viewer: 0, agent: 1, admin: 2 };
export function can(role: Role | undefined, needed: Role): boolean {
  return role !== undefined && ROLE_RANK[role] >= ROLE_RANK[needed];
}
