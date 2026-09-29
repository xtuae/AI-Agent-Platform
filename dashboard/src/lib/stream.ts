// Live updates over GET /api/v1/stream (Server-Sent Events), read with fetch() so the access
// token travels in the Authorization header and never in a URL. Events carry ids only; we
// invalidate the matching TanStack queries and let them refetch. While the stream is down the
// queries poll every 15 s instead (see usePollInterval).

import { useSyncExternalStore } from "react";
import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { currentSession, refreshSession } from "./api";

export interface SseFrame {
  event: string;
  data: string;
}

/** Split an SSE byte stream into frames. Returns the frames and the unparsed remainder. */
export function parseSse(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  const normalised = buffer.replace(/\r\n/g, "\n");
  const parts = normalised.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    let event = "message";
    const data: string[] = [];
    for (const line of part.split("\n")) {
      if (!line || line.startsWith(":")) continue; // comment / heartbeat
      const i = line.indexOf(":");
      const field = i === -1 ? line : line.slice(0, i);
      const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length || event !== "message") frames.push({ event, data: data.join("\n") });
  }
  return { frames, rest };
}

export interface ChangeEvent {
  entity: "customers" | "orders" | "conversations" | "messages" | "appointments" | "listings" | "resync";
  id: string | null;
  op: string | null;
  parent: string | null;
}

/** Which cached queries a change makes stale. */
export function keysFor(ev: ChangeEvent): QueryKey[] {
  switch (ev.entity) {
    case "orders":
      return [["orders"], ["order", ev.id], ["delivery"], ["today"], ["customer", ev.parent]];
    case "customers":
      return [["customers"], ["customer", ev.id]];
    case "appointments":
      return [["appointments"], ["appointment", ev.id], ["today"], ["customer", ev.parent]];
    case "listings":
      return [["listings"], ["today"]];
    case "conversations":
      return [["conversations"], ["thread", ev.id], ["today"]];
    case "messages":
      return [["conversations"], ["thread", ev.parent], ["today"]];
    default:
      return [[]]; // resync: everything
  }
}

// ---------------------------------------------------------------- connection state store

let live = false;
const subscribers = new Set<() => void>();
function setLive(v: boolean) {
  if (live !== v) {
    live = v;
    subscribers.forEach((s) => s());
  }
}
export function useLive(): boolean {
  return useSyncExternalStore(
    (cb) => {
      subscribers.add(cb);
      return () => subscribers.delete(cb);
    },
    () => live,
  );
}
/** 15 s polling fallback while live updates are unavailable. */
export function usePollInterval(): number | false {
  return useLive() ? false : 15_000;
}

// ---------------------------------------------------------------- the connection loop

export function startLiveUpdates(client: QueryClient): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let backoff = 2_000;
  let connectedBefore = false;
  let pending = new Map<string, QueryKey>();
  let flushTimer: ReturnType<typeof setTimeout> | null = null;

  const flush = () => {
    flushTimer = null;
    const keys = [...pending.values()];
    pending = new Map();
    for (const key of keys) {
      if (key.length === 0) void client.invalidateQueries();
      else void client.invalidateQueries({ queryKey: key });
    }
  };
  const schedule = (keys: QueryKey[]) => {
    for (const k of keys) {
      if (k.some((part) => part === null)) continue;
      pending.set(JSON.stringify(k), k);
    }
    // coalesce a burst (a turn writes several rows) into one round of refetches
    flushTimer ??= setTimeout(flush, 250);
  };

  const run = async () => {
    while (!stopped) {
      let token = currentSession()?.access_token;
      if (!token) token = (await refreshSession())?.access_token;
      if (!token) {
        setLive(false);
        return; // signed out; AuthProvider restarts us on the next login
      }
      controller = new AbortController();
      try {
        const r = await fetch("/api/v1/stream", {
          headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
          credentials: "same-origin",
          signal: controller.signal,
          cache: "no-store",
        });
        if (r.status === 401) {
          await refreshSession();
          continue;
        }
        if (!r.ok || !r.body) throw new Error(`stream ${r.status}`);
        const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        let expired = false;
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          const { frames, rest } = parseSse(buffer);
          buffer = rest;
          for (const f of frames) {
            if (f.event === "ready") {
              setLive(true);
              backoff = 2_000;
              if (connectedBefore) schedule([[]]); // we may have missed changes while away
              connectedBefore = true;
            } else if (f.event === "change") {
              try {
                schedule(keysFor(JSON.parse(f.data) as ChangeEvent));
              } catch {
                /* ignore a malformed frame */
              }
            } else if (f.event === "expired") {
              expired = true;
            }
          }
        }
        if (expired) {
          await refreshSession();
          continue; // reconnect immediately with the new token
        }
        throw new Error("stream closed");
      } catch {
        if (stopped) return;
        setLive(false);
        await new Promise((res) => setTimeout(res, backoff));
        backoff = Math.min(backoff * 2, 30_000);
      }
    }
  };
  void run();

  return () => {
    stopped = true;
    controller?.abort();
    if (flushTimer) clearTimeout(flushTimer);
    setLive(false);
  };
}
