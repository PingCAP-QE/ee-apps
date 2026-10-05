import { describe, expect, it } from "vitest";
import { listNeedsPolling } from "./ListPage";

describe("list polling", () => {
  const polling = { intervalMs: 10000, whilePointer: "/status/status", whileValues: ["PENDING", "PROCESSING"] };

  it("runs only while the list contains active work", () => {
    expect(listNeedsPolling([{ status: { status: "PROCESSING" } }], polling)).toBe(true);
    expect(listNeedsPolling([{ status: { status: "SUCCESS" } }], polling)).toBe(false);
  });
});
