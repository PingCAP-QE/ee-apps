import { describe, expect, it } from "vitest";
import type { DetailPageSpec } from "../types";
import { nextPollDelay, shouldPoll } from "./DetailPage";

const spec = {
  polling: { intervalMs: 5000, maxIntervalMs: 30000, whilePointer: "/status", whileValues: ["PENDING", "PROCESSING"] },
} as DetailPageSpec;

describe("detail polling", () => {
  it("stops for terminal builds", () => {
    expect(shouldPoll({ status: "PROCESSING" }, spec)).toBe(true);
    expect(shouldPoll({ status: "SUCCESS" }, spec)).toBe(false);
  });

  it("backs off repeated failures to the configured ceiling", () => {
    expect(nextPollDelay(5000, 30000, 0)).toBe(5000);
    expect(nextPollDelay(5000, 30000, 2)).toBe(20000);
    expect(nextPollDelay(5000, 30000, 10)).toBe(30000);
  });
});
