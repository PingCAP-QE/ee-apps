import assert from "node:assert/strict";
import test from "node:test";

import {
  COST_DATA_LAG_DAYS,
  getLaggedTrailingDateRange,
  getLatestBranchValue,
  getPreviousCompleteMondayWeek,
  getStableCostSummaryWeek,
} from "./api.js";

test("Cost trailing dates share the T+3 default lag", () => {
  assert.equal(COST_DATA_LAG_DAYS, 3);
  assert.deepEqual(getLaggedTrailingDateRange(new Date("2026-06-12T12:00:00")), {
    start_date: "2026-06-03",
    end_date: "2026-06-09",
  });
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
