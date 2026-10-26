import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createServer } from "vite";

let CICostWeeklyPage;
let LabeledDonutShareChart;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: CICostWeeklyPage } = await server.ssrLoadModule("/src/pages/CICostWeeklyPage.jsx"));
  ({ LabeledDonutShareChart } = await server.ssrLoadModule("/src/components/charts.jsx"));
});

after(async () => {
  await server?.close();
});

function account(vendor, total, items, missingDates = []) {
  return {
    vendor,
    cost_source: vendor === "gcp" ? "gcp:pingcap-testing-account" : "tencent:100050658403",
    cost_basis: vendor === "gcp" ? "list_cost" : "net_cost",
    total_cost: total,
    items: items.map(([name, value, sharePct, key = name]) => ({
      key,
      name,
      value,
      share_pct: sharePct ?? (total ? value / total * 100 : 0),
      evidence: `Billing evidence for ${name}`,
    })),
    observed_dates: ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
      .filter((date) => !missingDates.includes(date)),
    missing_dates: missingDates,
  };
}

async function withReport(accounts, check) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return {
      ok: true,
      json: async () => ({
        accounts: [],
        last_complete_week: { start_date: "2026-10-05", end_date: "2026-10-11" },
        last_complete_month: { start_date: "2026-09-01", end_date: "2026-09-30" },
        last_week_category_share: { accounts },
      }),
    };
  };
  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(CICostWeeklyPage));
      await Promise.resolve();
    });
    assert.deepEqual(requests, [
      "/api/v1/pages/ci-weekly-cost?section=overview",
      "/api/v1/pages/ci-weekly-cost?section=share",
      "/api/v1/pages/ci-weekly-cost?section=categories",
    ]);
    await check(renderer);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
}

test("category pies strictly exclude shares <= 1% without renormalizing or dropping table rows", async () => {
  await withReport([
    account("gcp", 100, [
      ["GKE", 97.8999],
      ["Just above one percent", 1.0001, 1],
      ["Exactly one percent", 1],
      ["Small category", 0.1],
    ], ["2026-10-11"]),
    account("tencent", 20, [["TKE", 18], ["CBS", 2]]),
  ], (renderer) => {
    const charts = renderer.root.findAllByType(LabeledDonutShareChart)
      .filter((chart) => chart.props.title.endsWith("categories"));
    assert.equal(charts.length, 2);
    assert.equal(charts[0].props.title, "GCP list cost categories");
    assert.equal(charts[1].props.title, "Tencent net cost categories");
    assert.deepEqual(charts[0].props.items.map((item) => item.name), ["GKE", "Just above one percent"]);
    assert.equal(charts[0].props.totalValue, 100);
    assert.equal(charts[0].props.centerValue, "$100.00");
    assert.equal(charts[0].props.items.reduce((sum, item) => sum + item.value, 0), 98.9);

    const svg = renderer.root.findByProps({ role: "img", "aria-label": "GCP list cost categories share chart" });
    assert.equal(svg.findAllByType("path").length, 2);
    const table = renderer.root.findByProps({ "aria-label": "GCP list cost categories table" });
    assert.equal(table.findByType("tbody").findAllByType("tr").length, 4);
    const rendered = JSON.stringify(renderer.toJSON());
    assert.match(rendered, /Exactly one percent/);
    assert.match(rendered, /Small category/);
    assert.match(rendered, /0.10%/);
    assert.equal(table.findByType("tfoot").findAllByType("td")[0].children.join(""), "$100.00");
    const status = renderer.root.findByProps({ role: "status" });
    assert.match(status.children.join(""), /6\/7 dates/);
    assert.match(status.children.join(""), /2026-10-11/);
    assert.match(JSON.stringify(renderer.toJSON()), /not self-hosted/);

    const panels = renderer.root.findAllByType("section").filter((section) => section.props.className === "panel");
    assert.equal(panels.at(-1).findByType("h3").children.join(""), "Last week's cost categories");
  });
});

test("category tables retain cents for large account totals", async () => {
  await withReport([account("gcp", 3638.3, [["GKE", 1783.41], ["Other", 1854.89]])], (renderer) => {
    const table = renderer.root.findByProps({ "aria-label": "GCP list cost categories table" });
    assert.equal(table.findByType("tfoot").findAllByType("td")[0].children.join(""), "$3,638.30");
    assert.equal(table.findByType("tbody").findAllByType("td")[0].children.join(""), "$1,783.41");
  });
});

test("dynamic CI subtotals sum compute and disks without adding a pie slice or changing total", async () => {
  await withReport([
    account("gcp", 1000, [
      ["GKE", 490.2, 49.02, "gke"],
      ["PVC", 146.4, 14.64, "gcp_pvc"],
      ["Networking", 13.1, 1.31, "networking"],
      ["OpenClaw", 11.1, 1.11, "openclaw"],
      ["Other", 339.2, 33.92, "other"],
    ]),
    account("tencent", 2000, [
      ["TKE / Serverless", 1668.2, 83.41, "tke"],
      ["CBS", 236, 11.8, "cbs"],
      ["Bazel", 47.6, 2.38, "tencent_bazel"],
      ["CVM", 11.8, 0.59, "cvm"],
      ["Other", 36.4, 1.82, "other"],
    ]),
  ], (renderer) => {
    for (const [title, cost, share, sliceCount, totalCost] of [
      ["GCP list cost categories", "$636.60", "63.66%", 5, "$1,000.00"],
      ["Tencent net cost categories", "$1,904.20", "95.21%", 4, "$2,000.00"],
    ]) {
      const table = renderer.root.findByProps({ "aria-label": `${title} table` });
      const subtotal = table.findByProps({ "aria-label": "Dynamic CI resources subtotal" });
      assert.deepEqual(subtotal.findAllByType("td").slice(0, 2).map((cell) => cell.children.join("")), [cost, share]);
      assert.equal(table.findByType("tfoot").findAllByType("td")[0].children.join(""), totalCost);
      assert.equal(table.findByType("tbody").findAllByType("tr").length, 5);
      const chart = renderer.root.findByProps({ role: "img", "aria-label": `${title} share chart` });
      assert.equal(chart.findAllByType("path").length, sliceCount);
    }
  });
});

test("dynamic CI subtotal includes small disk costs and rounds only after summing", async () => {
  await withReport([account("gcp", 100, [
    ["Compute", 50.0049, 50, "gke"],
    ["PVC", 0.0049, 0, "gcp_pvc"],
    ["Other", 49.9902, 50, "other"],
  ])], (renderer) => {
    const row = renderer.root.findByProps({ "aria-label": "Dynamic CI resources subtotal" });
    assert.deepEqual(row.findAllByType("td").slice(0, 2).map((cell) => cell.children.join("")), ["$50.01", "50.01%"]);
  });
});

test("floating-point sums do not hide positive category pies", async () => {
  await withReport([account("gcp", 0.6, [["Compute", 0.1], ["Storage", 0.2], ["Network", 0.3]])], (renderer) => {
    const chart = renderer.root.findByProps({ role: "img", "aria-label": "GCP list cost categories share chart" });
    assert.equal(chart.findAllByType("path").length, 3);
  });
});

test("negative adjustments remain in the table rather than producing an invalid pie", async () => {
  await withReport([account("tencent", 100, [["TKE", 120], ["Adjustment", -20]])], (renderer) => {
    assert.equal(renderer.root.findAllByProps({ role: "img", "aria-label": "Tencent net cost categories share chart" }).length, 0);
    assert.match(JSON.stringify(renderer.toJSON()), /Cost adjustments prevent a proportional pie/);
    const table = renderer.root.findByProps({ "aria-label": "Tencent net cost categories table" });
    assert.equal(table.findByType("tbody").findAllByType("td")[3].children.join(""), "-$20.00");
  });
});

test("empty accounts show no invented categories or percentages", async () => {
  await withReport([account("gcp", 0, []), account("tencent", 0, [])], (renderer) => {
    const tables = renderer.root.findAllByType("table");
    assert.equal(tables.length, 2);
    assert.ok(tables.every((table) => table.findByType("tbody").findAllByType("tr").length === 0));
    for (const table of tables) {
      const row = table.findByProps({ "aria-label": "Dynamic CI resources subtotal" });
      assert.deepEqual(row.findAllByType("td").slice(0, 2).map((cell) => cell.children.join("")), ["$0.00", "—"]);
    }
    assert.equal(renderer.root.findAllByType("svg").filter((svg) => String(svg.props["aria-label"]).endsWith("categories share chart")).length, 0);
    assert.match(JSON.stringify(renderer.toJSON()), /No categories > 1% available for GCP/);
  });
});
