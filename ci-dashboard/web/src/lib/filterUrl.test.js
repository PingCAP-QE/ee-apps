import assert from "node:assert/strict";
import test from "node:test";

import {
  buildDefaultFilters,
  buildFilterSearch,
  readFiltersFromSearch,
  sameFilters,
} from "./filterUrl.js";

const defaultRange = {
  start_date: "2026-05-01",
  end_date: "2026-06-01",
};

test("reads shareable filters from the current URL search", () => {
  const filters = readFiltersFromSearch(
    defaultRange,
    "/ci-status",
    "?repo=pingcap%2Ftidb&branch=master&job_name=pingcap%2Ftidb%2Fghpr_unit_test&cloud_phase=GCP&issue_status=closed&granularity=month&start_date=2026-05-25&end_date=2026-06-01",
  );

  assert.equal(filters.repo, "pingcap/tidb");
  assert.equal(filters.branch, "master");
  assert.equal(filters.job_name, "pingcap/tidb/ghpr_unit_test");
  assert.equal(filters.cloud_phase, "GCP");
  assert.equal(filters.issue_status, "closed");
  assert.equal(filters.start_date, "2026-05-25");
  assert.equal(filters.end_date, "2026-06-01");
  assert.equal(filters.granularity, "week");
});

test("serializes non-empty filters into request-compatible URL parameters", () => {
  const filters = {
    ...buildDefaultFilters(defaultRange, "/flaky"),
    job_name: "pingcap/tidb/ghpr_unit_test",
    cloud_phase: "GCP",
  };

  const search = buildFilterSearch(filters, "/flaky");
  const params = new URLSearchParams(search);

  assert.equal(params.get("repo"), "pingcap/tidb");
  assert.equal(params.get("branch"), "master");
  assert.equal(params.get("job_name"), "pingcap/tidb/ghpr_unit_test");
  assert.equal(params.get("cloud_phase"), "GCP");
  assert.equal(params.get("issue_status"), "closed");
  assert.equal(params.get("granularity"), "day");
});

test("keeps cost dashboard month buckets but normalizes invalid values", () => {
  assert.equal(
    readFiltersFromSearch(defaultRange, "/cost", "?granularity=month").granularity,
    "month",
  );
  assert.equal(
    readFiltersFromSearch(defaultRange, "/cost", "?granularity=day").granularity,
    "week",
  );
});

test("compares filter values without being sensitive to object identity", () => {
  assert.equal(
    sameFilters(
      { repo: "pingcap/tidb", branch: "master", start_date: "2026-05-25" },
      { repo: "pingcap/tidb", branch: "master", start_date: "2026-05-25" },
    ),
    true,
  );
  assert.equal(
    sameFilters(
      { repo: "pingcap/tidb", branch: "master" },
      { repo: "pingcap/tidb", branch: "release-8.5" },
    ),
    false,
  );
});
