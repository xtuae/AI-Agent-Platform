import { describe, expect, it } from "vitest";
import { keysFor } from "@/lib/stream";
import { addDays, dayLabel, hhmm, plusMinutes, weekStart } from "./shared";

describe("agenda date helpers", () => {
  it("works on tenant-local wall clock strings", () => {
    expect(hhmm("2030-01-08T09:30")).toBe("09:30");
    expect(plusMinutes("09:30", 45)).toBe("10:15");
    expect(plusMinutes("23:30", 60)).toBe("00:30");
  });
  it("steps days and finds the Monday of a week", () => {
    expect(addDays("2030-01-31", 1)).toBe("2030-02-01");
    expect(weekStart("2030-01-10")).toBe("2030-01-07"); // Thursday → Monday
    expect(weekStart("2030-01-13")).toBe("2030-01-07"); // Sunday belongs to the week before
    expect(dayLabel("2030-01-08")).toBe("Tue 8 Jan");
  });
});

describe("live updates", () => {
  it("an appointment change refreshes the agenda, Today and the contact", () => {
    expect(keysFor({ entity: "appointments", id: "a1", op: "update", parent: "c1" })).toEqual([
      ["appointments"],
      ["appointment", "a1"],
      ["today"],
      ["customer", "c1"],
    ]);
  });
});
