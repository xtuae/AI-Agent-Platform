import { describe, expect, it } from "vitest";
import { keysFor, parseSse } from "./stream";

describe("parseSse", () => {
  it("parses frames, skips heartbeats, keeps the partial tail", () => {
    const { frames, rest } = parseSse(
      'retry: 5000\n\nevent: ready\ndata: {}\n\n: ping\n\nevent: change\ndata: {"entity":"orders","id":"1"}\n\nevent: cha',
    );
    expect(frames).toEqual([
      { event: "ready", data: "{}" },
      { event: "change", data: '{"entity":"orders","id":"1"}' },
    ]);
    expect(rest).toBe("event: cha");
  });
  it("handles CRLF and multi-line data", () => {
    const { frames } = parseSse("event: change\r\ndata: a\r\ndata: b\r\n\r\n");
    expect(frames).toEqual([{ event: "change", data: "a\nb" }]);
  });
});

describe("keysFor", () => {
  it("a message refreshes its thread and the inbox", () => {
    expect(keysFor({ entity: "messages", id: "m", op: "insert", parent: "c1" })).toEqual([
      ["conversations"],
      ["thread", "c1"],
      ["today"],
    ]);
  });
  it("resync refreshes everything", () => {
    expect(keysFor({ entity: "resync", id: null, op: null, parent: null })).toEqual([[]]);
  });
});
