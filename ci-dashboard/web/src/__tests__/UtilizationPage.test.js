import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { MemoryRouter } from "react-router-dom";
import { createServer } from "vite";

let server;
let UtilizationPage;
let App;
let utilizationPercent;
let weeklyTrendPoints;
let TrendChart;

before(async () => {
  server = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  ({ default: UtilizationPage, utilizationPercent, weeklyTrendPoints } = await server.ssrLoadModule("/src/pages/UtilizationPage.jsx"));
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
  ({ TrendChart } = await server.ssrLoadModule("/src/components/charts.jsx"));
});

after(async () => { await server?.close(); });

const readyStatus = {
  available: true, configured: true, collection_enabled: true, stale: false,
  latest_cutoff: "2026-11-24T04:00:00Z", maintenance_stale: false,
  missing_daily_dates: ["2026-11-20"], collection_gaps: [{ status: "final_missing" }],
  paused: true, pause_reasons: ["maintenance unavailable"],
  daily_window: { start_date: "2026-11-16", end_date: "2026-11-24" },
};
const metric = {
  avg: null, peak: 143.12, coverage: 0.53, peak_coverage: 1,
  peak_subset: 190.1, peak_subset_coverage: 0.97, peak_subset_at: "2026-11-24T03:01:00Z",
  total_used_integral: 70, used_integral: 50, capacity_integral: 200,
  observed_seconds: 40, expected_seconds: 60,
  contract: { denominator: "requests", normalized_unit: "cores" },
  quality: ["unknown_requests"], peak_at: "2026-11-24T03:00:00Z",
};
const dailyData = {
  available: true, has_more: true, items: [{
    usage_date: "2026-11-24", scope: "job", resource_key: "stable-key",
    resource_class: "tke_native", resource_spec: "2c/4GiB", repo: "unknown_repo",
    job_name: "unknown_job", status: "partial", cutoff: "2026-11-24T04:00:00Z",
    evidence_cutoff: "2026-11-24T03:00:00Z", metrics: { cpu: metric },
  }],
};
const weeklyData = {
  available: true, has_more: false, items: [{
    week_start: "2026-11-16", resource_class: "tke_native", metric: "cpu",
    status: "final_missing", values: { avg: null, peak: null, coverage: null },
    contract: { denominator: "physical", normalized_unit: "cores" },
  }],
};
function field(renderer, label) {
  return renderer.root.findAllByType("label").find((node) => node.findByType("span").children.join("") === label)
    .findAll((node) => node.type === "input" || node.type === "select")[0];
}

for (const reason of ["disabled", "missing_schema", "database_unavailable", "unconfigured"]) {
  test(`${reason} is informative, queries only status, and preserves other App routes`, async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    let renderer;
    globalThis.fetch = async (url) => {
      requests.push(String(url));
      return { ok: true, json: async () => ({ available: reason === "unconfigured", configured: false, reason }) };
    };
    try {
      await act(async () => {
        renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: ["/utilization"] }, React.createElement(App)));
      });
      assert.deepEqual(requests, ["/api/v1/utilization/status"]);
      const content = JSON.stringify(renderer.toJSON());
      assert.match(content, /Resource Utilization/);
      assert.match(content, /Tencent CICD/);
      assert.doesNotMatch(content, /Mean 0\.0%/);
      assert.equal(renderer.root.findAllByType("input").length, 0, "no unrelated CI filters");
      const links = renderer.root.findAllByType("a");
      assert.ok(links.some((node) => node.props.href === "/utilization"));
      assert.ok(links.some((node) => node.props.href.startsWith("/cost")));
    } finally {
      await act(async () => renderer?.unmount());
      globalThis.fetch = originalFetch;
    }
  });
}

test("shows unknown / partial / cutoff / metric quality and never invents a zero or Job cost", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    const data = String(url).includes("/status") ? readyStatus
      : String(url).includes("/daily") ? dailyData : weeklyData;
    return { ok: true, json: async () => data };
  };
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.equal(requests.length, 3);
    assert.ok(requests[1].includes("scope=job"));
    assert.ok(requests[1].includes("start_date=2026-11-16"));
    assert.ok(requests[2].includes("resource_class=tke_native&metric=cpu"));
    const content = JSON.stringify(renderer.toJSON());
    for (const text of ["unknown_repo", "unknown_job", "partial", "unknown_requests", "Evidence:", "N/A", "143.1%", "53.0%", "Observed subset peak", "190.1%", "97.0%", "not a full-population peak", "final_missing", "requests", "physical", "2026-11-20", "Collection paused:"]) {
      assert.ok(content.includes(text), text);
    }
    assert.doesNotMatch(content, /Mean 0\.0%/);
    assert.match(content, /Precise Job cost: unavailable/);
    const chart = renderer.root.findByType(TrendChart);
    assert.deepEqual(chart.props.series[0].points, [["2026-11-16", null]]);
    assert.equal(chart.props.showTooltipSum, undefined);
    assert.equal(utilizationPercent(null), "N/A");
    assert.equal(utilizationPercent(0), "0.0%");
    assert.equal(utilizationPercent(1.43, true), "143.0%");

    await act(async () => field(renderer, "Repo").props.onChange({ target: { value: "pingcap/tidb" } }));
    assert.match(requests.at(-1), /repo=pingcap%2Ftidb/);
    await act(async () => field(renderer, "Stable Job").props.onChange({ target: { value: "/job/tidb/job/unit_test" } }));
    assert.match(requests.at(-1), /job_name=%2Fjob%2Ftidb%2Fjob%2Funit_test/);
    await act(async () => field(renderer, "Stored scope").props.onChange({ target: { value: "account_class" } }));
    assert.match(requests.at(-1), /scope=account_class/);
    assert.doesNotMatch(requests.at(-1), /repo=|job_name=|resource_spec=/);
    assert.ok(!renderer.root.findAllByType("label").some((node) => node.findByType("span").children.join("") === "Repo"));
    await act(async () => field(renderer, "Weekly metric").props.onChange({ target: { value: "memory" } }));
    assert.match(requests.at(-1), /metric=memory/);
    await act(async () => field(renderer, "Weekly resource class").props.onChange({ target: { value: "cvm" } }));
    assert.match(requests.at(-1), /resource_class=cvm/);
    const next = renderer.root.findAllByType("button").find((node) => node.children[0] === "Next");
    await act(async () => next.props.onClick());
    assert.match(requests.at(-1), /page=2/);
    assert.ok(requests.every((url) => url.startsWith("/api/v1/utilization/")));
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly chart inserts visible null gaps instead of connecting missing weeks", () => {
  assert.deepEqual(weeklyTrendPoints([]), []);
  assert.deepEqual(weeklyTrendPoints([
    { week_start: "2026-11-23", status: "final_missing", values: { avg: 99 } },
    { week_start: "2026-11-02", values: { avg: 0 } },
    { week_start: "2026-11-16", values: { avg: 70 } },
  ]), [["2026-11-02", 0], ["2026-11-09", null], ["2026-11-16", 70], ["2026-11-23", null]]);
});

test("failed daily request does not hide weekly history, and changing filters hides stale rows", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  let resolveDaily;
  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    if (String(url).includes("/daily")) return new Promise((resolve) => { resolveDaily = resolve; });
    return { ok: true, json: async () => String(url).includes("/status") ? readyStatus : weeklyData };
  };
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.match(JSON.stringify(renderer.toJSON()), /final_missing/);
    await act(async () => resolveDaily({ ok: true, json: async () => dailyData }));
    assert.match(JSON.stringify(renderer.toJSON()), /unknown_repo/);
    await act(async () => field(renderer, "Resource class").props.onChange({ target: { value: "cvm" } }));
    assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /"unknown_repo"/);
    await act(async () => resolveDaily({ ok: false, status: 503, statusText: "Unavailable" }));
    assert.match(JSON.stringify(renderer.toJSON()), /503 Unavailable/);
    assert.match(JSON.stringify(renderer.toJSON()), /final_missing/);
    assert.ok(requests.every((url) => url.startsWith("/api/v1/utilization/")));
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});
