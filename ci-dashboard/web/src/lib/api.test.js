import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchJson,
  getLatestBranchValue,
  getPreviousCompleteMondayWeek,
  getStableCostSummaryWeek,
} from "./api.js";

test("keeps non-OK JSON status and payload on the thrown error", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    statusText: "Conflict",
    json: async () => ({ error: { code: "unsupported_cost_basis" } }),
  });

  try {
    await assert.rejects(fetchJson("/ci-weekly-cost"), (error) => {
      assert.equal(error.message, "409 Conflict");
      assert.equal(error.status, 409);
      assert.deepEqual(error.payload, { error: { code: "unsupported_cost_basis" } });
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("returns the previous complete Monday through Sunday week", () => {
  assert.deepEqual(
    getPreviousCompleteMondayWeek(new Date("2026-06-12T12:00:00")),
    {
      start_date: "2026-06-01",
      end_date: "2026-06-07",
    },
  );
});

test("does not treat the current Sunday as a completed week", () => {
  assert.deepEqual(
    getPreviousCompleteMondayWeek(new Date("2026-06-14T12:00:00")),
    {
      start_date: "2026-06-01",
      end_date: "2026-06-07",
    },
  );
});

test("reads the latest distinct flaky count for the requested branch", () => {
  assert.equal(
    getLatestBranchValue(
      [
        { branch: "master", values: [33, 21] },
        { branch: "release-8.5", values: [38, 62] },
      ],
      "master",
    ),
    21,
  );
});

test("keeps cost on the week before last through Wednesday", () => {
  assert.deepEqual(
    getStableCostSummaryWeek(new Date("2026-06-10T12:00:00")),
    {
      start_date: "2026-05-25",
      end_date: "2026-05-31",
    },
  );
});

test("moves cost to the previous complete week on Thursday", () => {
  assert.deepEqual(
    getStableCostSummaryWeek(new Date("2026-06-11T12:00:00")),
    {
      start_date: "2026-06-01",
      end_date: "2026-06-07",
    },
  );
});
