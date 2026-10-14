import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  buildDefaultFilters,
  buildFilterSearch,
  buildNavSearchByPath,
  getCostBucketOptions,
  getLatestCostDate,
  normalizeFiltersForPath,
  readFiltersFromSearch,
  sameFilters,
  MIGRATE_STATUS_PATH,
  WEEKLY_COST_PATH,
} from "./filterUrl.js";

const defaultRange = {
  start_date: "2026-05-01",
  end_date: "2026-06-01",
};

beforeEach((context) => {
  context.mock.timers.enable({ apis: ["Date"], now: Date.UTC(2031, 9, 14, 12) });
});

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
  assert.equal(filters.granularity, "month");
});

test("keeps the selected bucket on the CI Status page", () => {
  assert.equal(
    readFiltersFromSearch(defaultRange, "/ci-status", "?granularity=day").granularity,
    "day",
  );
  assert.equal(
    readFiltersFromSearch(defaultRange, "/ci-status", "?granularity=month").granularity,
    "month",
  );
});

test("allows day buckets on the Tencent Migration page", () => {
  assert.equal(buildDefaultFilters(defaultRange, MIGRATE_STATUS_PATH).granularity, "day");
  assert.equal(
    readFiltersFromSearch(defaultRange, MIGRATE_STATUS_PATH, "?granularity=day").granularity,
    "day",
  );
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

test("weekly summary never serializes filters into the root URL", () => {
  const search = buildFilterSearch(
    {
      ...buildDefaultFilters(defaultRange, "/"),
      repo: "pingcap/tidb",
      start_date: "2026-05-01",
      end_date: "2026-06-01",
    },
    "/",
  );

  assert.equal(search, "");
});

test("QA Cost Weekly never serializes filters into its fixed-report URL", () => {
  assert.equal(WEEKLY_COST_PATH, "/qa-cost-weekly");

  const search = buildFilterSearch(
    {
      ...buildDefaultFilters(defaultRange, WEEKLY_COST_PATH),
      repo: "pingcap/tidb",
      start_date: "2026-05-01",
      end_date: "2026-06-01",
    },
    WEEKLY_COST_PATH,
  );

  assert.equal(search, "");
});

test("defaults the cost tab to T+3 data availability", () => {
  assert.deepEqual(buildDefaultFilters(defaultRange, "/cost"), {
    repo: "",
    branch: "",
    job_name: "",
    cloud_phase: "",
    issue_status: "",
    cost_source: "gcp:pingcap-testing-account",
    owner_include: "",
    owner_exclude: "",
    team_include: "",
    team_exclude: "",
    project_include: "",
    project_exclude: "",
    granularity: "week",
    start_date: "2026-05-01",
    end_date: "2026-05-29",
  });

  const navSearchByPath = buildNavSearchByPath(
    {},
    defaultRange,
    buildDefaultFilters(defaultRange, "/ci-status"),
  );
  const costParams = new URLSearchParams(navSearchByPath["/cost"]);
  assert.equal(costParams.get("start_date"), "2026-05-01");
  assert.equal(costParams.get("end_date"), "2026-05-29");
});

test("latest Cost date uses three UTC calendar days across month and year boundaries", () => {
  for (const [reference, expected] of [
    ["2026-10-14T07:00:00Z", "2026-10-11"],
    ["2026-01-02T23:00:00Z", "2025-12-30"],
    ["2024-03-03T00:00:00Z", "2024-02-29"],
  ]) {
    assert.equal(getLatestCostDate(new Date(reference)), expected);
  }
});

test("Cost End is capped at T+3 before requests, without changing other tabs", () => {
  const filters = { start_date: "2031-10-01", end_date: "2031-10-14", granularity: "day" };
  assert.equal(normalizeFiltersForPath("/cost", filters).end_date, "2031-10-11");
  assert.equal(normalizeFiltersForPath("/ci-status", filters).end_date, "2031-10-14");
  assert.equal(new URLSearchParams(buildFilterSearch(filters, "/cost")).get("end_date"), "2031-10-11");
});

test("keeps cost dashboard month buckets and the week default", () => {
  assert.equal(
    readFiltersFromSearch(defaultRange, "/cost", "?granularity=month").granularity,
    "month",
  );
  assert.equal(
    readFiltersFromSearch(defaultRange, "/cost", "?granularity=invalid").granularity,
    "week",
  );
  assert.equal(
    readFiltersFromSearch(defaultRange, "/cost", "?granularity=day").granularity,
    "week",
  );
});

test("Cost Day counts both endpoints at the 14/15-day boundary", () => {
  for (const [end_date, expected] of [
    ["2026-07-01", "day"],
    ["2026-07-14", "day"],
    ["2026-07-15", "week"],
  ]) {
    const filters = readFiltersFromSearch(defaultRange, "/cost",
      `?start_date=2026-07-01&end_date=${end_date}&granularity=day`);
    assert.equal(filters.granularity, expected);
  }
});

test("Cost Week uses a strict calendar-month limit, not a 60-day limit", () => {
  for (const [end_date, expected] of [
    ["2026-08-30", "week"],
    ["2026-08-31", "week"],
    ["2026-09-01", "month"],
    ["2026-09-02", "month"],
  ]) {
    const filters = readFiltersFromSearch(defaultRange, "/cost",
      `?start_date=2026-07-01&end_date=${end_date}&granularity=week`);
    assert.equal(filters.granularity, expected);
  }
});

test("Cost Week preserves day-of-month and clamps across February and year rollover", () => {
  const boundaries = [
    ["2026-01-31", "2026-03-30", "2026-03-31"],
    ["2025-12-31", "2026-02-27", "2026-02-28"],
    ["2023-12-31", "2024-02-28", "2024-02-29"],
    ["2026-07-31", "2026-09-29", "2026-09-30"],
    ["2026-08-31", "2026-10-30", "2026-10-31"],
    ["2026-11-30", "2027-01-29", "2027-01-30"],
    ["2026-02-28", "2026-04-27", "2026-04-28"],
    ["2024-02-29", "2024-04-28", "2024-04-29"],
  ];
  for (const [start_date, beforeLimit, limit] of boundaries) {
    assert.equal(normalizeFiltersForPath("/cost", {
      start_date, end_date: beforeLimit, granularity: "week",
    }).granularity, "week", `${start_date} through ${beforeLimit}`);
    assert.equal(normalizeFiltersForPath("/cost", {
      start_date, end_date: limit, granularity: "week",
    }).granularity, "month", `${start_date} through ${limit}`);
  }
});

test("Cost normalization only coarsens invalid buckets and never refines on narrower dates", () => {
  let filters = normalizeFiltersForPath("/cost", {
    start_date: "2026-07-01", end_date: "2026-07-14", granularity: "day",
  });
  assert.equal(filters.granularity, "day");
  filters = normalizeFiltersForPath("/cost", { ...filters, end_date: "2026-07-15" });
  assert.equal(filters.granularity, "week");
  filters = normalizeFiltersForPath("/cost", { ...filters, end_date: "2026-07-14" });
  assert.equal(filters.granularity, "week");
  filters = normalizeFiltersForPath("/cost", { ...filters, end_date: "2026-09-01" });
  assert.equal(filters.granularity, "month");
  filters = normalizeFiltersForPath("/cost", { ...filters, end_date: "2026-07-14" });
  assert.equal(filters.granularity, "month");
  assert.equal(normalizeFiltersForPath("/cost", {
    ...filters, end_date: "2030-07-14", granularity: "day",
  }).granularity, "month");
});

test("missing, malformed, impossible or reversed Cost dates disable restricted buckets", () => {
  const invalidRanges = [
    {},
    { start_date: "", end_date: "2026-07-01" },
    { start_date: "2026-07-01" },
    { start_date: "2026-7-01", end_date: "2026-07-01" },
    { start_date: "not-a-date", end_date: "2026-07-01" },
    { start_date: "2026-02-29", end_date: "2026-03-01" },
    { start_date: "2026-07-01", end_date: "2026-13-01" },
    { start_date: "0000-01-01", end_date: "0000-01-02" },
    { start_date: "2026-07-02", end_date: "2026-07-01" },
  ];
  for (const range of invalidRanges) {
    const options = getCostBucketOptions(range);
    assert.deepEqual(options.map((option) => option.disabled), [true, true, false]);
    assert.match(options[0].reason, /valid start and end dates/);
    assert.equal(normalizeFiltersForPath("/cost", { ...range, granularity: "day" }).granularity, "month");
  }
  assert.equal(getCostBucketOptions({
    start_date: "2024-02-29", end_date: "2024-03-13",
  })[0].disabled, false);
});

test("Cost bookmark serialization and navigation synchronously coarsen illegal ranges", () => {
  const longRange = { start_date: "2026-07-01", end_date: "2026-09-01" };
  const costFilters = { ...buildDefaultFilters(defaultRange, "/cost"), ...longRange, granularity: "day" };
  assert.equal(new URLSearchParams(buildFilterSearch(costFilters, "/cost")).get("granularity"), "month");
  assert.equal(readFiltersFromSearch(defaultRange, "/cost", buildFilterSearch(costFilters, "/cost")).granularity, "month");
  const nav = buildNavSearchByPath({ "/cost": costFilters }, defaultRange, {
    ...buildDefaultFilters(defaultRange, "/ci-status"), ...longRange,
  });
  assert.equal(new URLSearchParams(nav["/cost"]).get("granularity"), "month");
  assert.equal(new URLSearchParams(nav["/ci-status"]).get("granularity"), "day");
  assert.equal(new URLSearchParams(nav["/runtime-insights"]).get("granularity"), "week");
});

test("keeps cost source on the cost tab and drops unrelated filters", () => {
  const filters = readFiltersFromSearch(
    defaultRange,
    "/cost",
    "?repo=pingcap%2Ftidb&branch=main&job_name=unit&cloud_phase=GCP&issue_status=open&cost_source=aws%3A946646677266&granularity=month",
  );

  assert.equal(filters.repo, "");
  assert.equal(filters.branch, "");
  assert.equal(filters.job_name, "");
  assert.equal(filters.cloud_phase, "");
  assert.equal(filters.issue_status, "");
  assert.equal(filters.cost_source, "aws:946646677266");
  assert.equal(filters.granularity, "month");
});

test("does not keep cost source outside the cost tab", () => {
  const filters = readFiltersFromSearch(
    defaultRange,
    "/ci-status",
    "?cost_source=aws%3A946646677266",
  );

  assert.equal(filters.cost_source, "");
});

test("serializes cost source for cost links", () => {
  const search = buildFilterSearch(
    {
      ...buildDefaultFilters(defaultRange, "/cost"),
      cost_source: "aws:946646677266",
      granularity: "month",
    },
    "/cost",
  );
  const params = new URLSearchParams(search);

  assert.equal(params.get("cost_source"), "aws:946646677266");
  assert.equal(params.get("granularity"), "month");
});

test("keeps shareable cost dimension include and exclude filters", () => {
  const filters = readFiltersFromSearch(
    defaultRange,
    "/cost",
    "?cost_source=aws%3Aqa%2Cgcp%3Aci&owner_include=alice%2Cbob&team_exclude=TiKV&project_include=alpha",
  );

  assert.equal(filters.cost_source, "aws:qa,gcp:ci");
  assert.equal(filters.owner_include, "alice,bob");
  assert.equal(filters.team_exclude, "TiKV");
  assert.equal(filters.project_include, "alpha");
  const params = new URLSearchParams(buildFilterSearch(filters, "/cost"));
  assert.equal(params.get("owner_include"), "alice,bob");
  assert.equal(params.get("team_exclude"), "TiKV");
  assert.equal(params.get("project_include"), "alpha");
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

test("keeps the active date range when building links to other tabs", () => {
  const navSearchByPath = buildNavSearchByPath(
    {
      "/flaky": {
        ...buildDefaultFilters(defaultRange, "/flaky"),
        start_date: "2026-04-01",
        end_date: "2026-04-30",
      },
    },
    defaultRange,
    {
      ...buildDefaultFilters(defaultRange, "/ci-status"),
      start_date: "2026-05-25",
      end_date: "2026-06-01",
      repo: "pingcap/tidb",
      branch: "master",
    },
  );

  const flakyParams = new URLSearchParams(navSearchByPath["/flaky"]);
  const migrateParams = new URLSearchParams(navSearchByPath["/migrate-status"]);
  assert.equal(flakyParams.get("start_date"), "2026-05-25");
  assert.equal(flakyParams.get("end_date"), "2026-06-01");
  assert.equal(flakyParams.get("repo"), "pingcap/tidb");
  assert.equal(flakyParams.get("issue_status"), "closed");
  assert.equal(migrateParams.get("start_date"), "2026-05-25");
  assert.equal(migrateParams.get("end_date"), "2026-06-01");
  assert.equal(migrateParams.get("granularity"), "day");
});
