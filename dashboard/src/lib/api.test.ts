import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, buildUrl, can, currentSession, refreshSession, setSession, type Session } from "./api";

const session = (token: string): Session => ({
  access_token: token,
  expires_at: "2030-01-01T00:00:00Z",
  user: { id: "u", email: "a@b.c", name: null, role: "agent" },
  tenant: {
    id: "t",
    name: "T",
    slug: "t",
    timezone: "Asia/Dubai",
    meta_charges_borne_by_us_until: null,
    modules: ["catalog", "orders"],
    contact_label: "Customers",
  },
});

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => setSession(null));

describe("api()", () => {
  it("sends the bearer token and never puts it in the URL", async () => {
    setSession(session("tok-1"));
    const f = vi.fn(async () => json(200, { ok: true }));
    await api("/orders", { query: { q: "x" } }, f as unknown as typeof fetch);
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/orders?q=x");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-1");
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    setSession(session("old"));
    const f = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/auth/refresh")) {
        expect((init?.headers as Record<string, string>)["X-HMH-CSRF"]).toBe("1");
        return json(200, session("new"));
      }
      const auth = (init?.headers as Record<string, string>).Authorization;
      return auth === "Bearer new" ? json(200, { n: 1 }) : json(401, { detail: "expired" });
    });
    await expect(api("/today", {}, f as unknown as typeof fetch)).resolves.toEqual({ n: 1 });
    expect(currentSession()?.access_token).toBe("new");
  });

  it("concurrent 401s share one refresh request", async () => {
    setSession(session("old"));
    let refreshes = 0;
    const f = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/auth/refresh")) {
        refreshes += 1;
        await new Promise((r) => setTimeout(r, 10));
        return json(200, session("new"));
      }
      const auth = (init?.headers as Record<string, string>).Authorization;
      return auth === "Bearer new" ? json(200, {}) : json(401, {});
    });
    const ff = f as unknown as typeof fetch;
    await Promise.all([api("/a", {}, ff), api("/b", {}, ff), api("/c", {}, ff)]);
    expect(refreshes).toBe(1);
  });

  it("a failed refresh signs out and surfaces a 401", async () => {
    setSession(session("old"));
    const f = vi.fn(async () => json(401, { detail: "session ended" }));
    await expect(api("/today", {}, f as unknown as typeof fetch)).rejects.toMatchObject({ status: 401 });
    expect(currentSession()).toBeNull();
  });

  it("turns FastAPI errors into readable messages with codes", async () => {
    const f = vi.fn(async () => json(409, { detail: { code: "take_over_first" } }));
    const err = await api("/x", {}, f as unknown as typeof fetch).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("take_over_first");
    expect((err as ApiError).message).toBe("take over first");
    const v = new ApiError(422, [{ msg: "Value error, not a valid international phone number" }]);
    expect(v.message).toBe("not a valid international phone number");
  });
});

describe("helpers", () => {
  it("buildUrl skips empties and repeats arrays", () => {
    expect(buildUrl("/orders", { status: ["confirmed", "draft"], area: "", q: undefined, limit: 5 })).toBe(
      "/api/v1/orders?status=confirmed&status=draft&limit=5",
    );
  });
  it("role ranks", () => {
    expect(can("admin", "agent")).toBe(true);
    expect(can("viewer", "agent")).toBe(false);
    expect(can(undefined, "viewer")).toBe(false);
  });
  it("refreshSession with no cookie yields null", async () => {
    const f = vi.fn(async () => json(401, {}));
    await expect(refreshSession(f as unknown as typeof fetch)).resolves.toBeNull();
  });
});
