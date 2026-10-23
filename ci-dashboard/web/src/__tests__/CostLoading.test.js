import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createServer } from "vite";

let CostPage;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(), appType: "custom", logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: CostPage } = await server.ssrLoadModule("/src/pages/CostPage.jsx"));
});

after(async () => server?.close());

const filters = {
  start_date: "2026-10-01", end_date: "2026-10-20", granularity: "week",
  cost_source: "gcp:pingcap-testing-account",
};
const reports = {
  share: { items: [{ name: "alice", value: 123, share_pct: 100 }], meta: { total_list_cost: 123 } },
  "repo-group-stack": {
    items: [{ name: "alice", value: 123 }],
    series: [{ key: "alice", label: "alice", type: "bar", points: [["2026-10-12", 123]] }],
  },
  trend: {
    series: [{ key: "net_cost", label: "Net cost", type: "line", points: [["2026-10-12", 100]] }],
    meta: { summary: { list_cost: 123, net_cost: 100 } },
  },
  "engineering-group-share": { level1: { items: [] }, level2: { items: [] } },
};

async function withPage(check) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = (url, { signal }) => new Promise((resolve) => {
    requests.push({ url: String(url), signal, resolve, settled: false });
  });
  const paths = () => requests.map(({ url }) => new URL(url, "http://localhost").pathname);
  const settle = async (name, failed = false) => {
    const request = requests.findLast(({ url, settled }) => !settled
      && new URL(url, "http://localhost").pathname === `/api/v1/pages/cost-${name}`);
    assert.ok(request, `Missing request: ${name}`);
    request.settled = true;
    await act(async () => request.resolve({
      ok: !failed, status: 503, statusText: "Service Unavailable",
      json: async () => reports[name],
    }));
  };
  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(CostPage, { filters }));
    });
    await check({ renderer, requests, paths, settle });
  } finally {
    await act(async () => renderer?.unmount());
    assert.ok(requests.every(({ signal }) => signal.aborted));
    globalThis.fetch = originalFetch;
  }
}

for (const first of ["share", "trend"]) {
  test(`Cost renders ${first} independently and defers engineering queries`, async () => {
    await withPage(async ({ renderer, paths, settle }) => {
      assert.deepEqual(paths(), [
        "/api/v1/pages/cost-trend", "/api/v1/pages/cost-share",
        "/api/v1/pages/cost-repo-group-stack",
      ]);
      const share = () => renderer.root.findByProps({ role: "region", "aria-label": "Cost share" });
      const trend = () => renderer.root.findByProps({ "aria-label": "Cost trend" });
      if (first === "share") {
        await settle("share");
        assert.equal(share().findAllByType("svg").length, 1);
        assert.equal(trend().findAllByType("svg").length, 0);
      } else {
        await settle("repo-group-stack");
        await settle("trend");
        assert.equal(trend().findAllByType("svg").length, 1);
        assert.equal(share().findAllByType("svg").length, 0);
      }
      assert.equal(paths().length, 3);
      if (first === "share") {
        await settle("repo-group-stack");
        await settle("trend");
      } else {
        await settle("share");
      }
      assert.equal(share().findAllByType("svg").length, 1);
      assert.equal(trend().findAllByType("svg").length, 1);
      assert.equal(paths().at(-1), "/api/v1/pages/cost-engineering-group-share");
      await settle("engineering-group-share");
      assert.ok(!paths().includes("/api/v1/pages/cost-unmatched-resources"));
    });
  });
}

test("Cost summary shows pending values rather than zero while spend is loading", async () => {
  await withPage(async ({ renderer, settle }) => {
    const values = () => renderer.root.findAllByProps({ className: "stat-card__value" })
      .map(card => card.children.join(""));
    assert.deepEqual(values(), ["—", "—", "—"]);
    await settle("trend");
    assert.deepEqual(values().slice(0, 2), ["$100.00", "$123.00"]);
  });
});

test("Cost share can request resources while the trend is still pending", async () => {
  await withPage(async ({ renderer, paths, settle }) => {
    await settle("share");
    const owner = renderer.root.findByProps({ className: "donut-legend__name" });
    await act(async () => owner.parent.props.onClick());
    assert.equal(paths().at(-1), "/api/v1/pages/cost-unmatched-resources");
    assert.ok(!paths().includes("/api/v1/pages/cost-engineering-group-share"));
  });
});

for (const failed of ["share", "trend", "repo-group-stack"]) {
  test(`Cost keeps sibling charts visible when ${failed} fails`, async () => {
    await withPage(async ({ renderer, paths, settle }) => {
      await settle("share", failed === "share");
      await settle("trend", failed === "trend");
      await settle("repo-group-stack", failed === "repo-group-stack");
      const share = renderer.root.findByProps({ role: "region", "aria-label": "Cost share" });
      const trend = renderer.root.findByProps({ "aria-label": "Cost trend" });
      assert.equal(share.findAllByType("svg").length, failed === "share" ? 0 : 1);
      assert.equal(trend.findAllByType("svg").length, failed === "share" ? 1 : 0);
      assert.match(JSON.stringify(renderer.toJSON()), /503 Service Unavailable/);
      assert.equal(paths().at(-1), "/api/v1/pages/cost-engineering-group-share");
      await settle("engineering-group-share");
    });
  });
}

test("Cost does not refetch Engineering Group shares when drilling into a team", async () => {
  await withPage(async ({ renderer, paths, settle }) => {
    for (const name of ["trend", "share", "repo-group-stack", "engineering-group-share"]) {
      await settle(name);
    }
    await act(async () => renderer.update(React.createElement(CostPage, {
      filters, costBreakdownGroupBy: "team",
    })));
    await settle("share");
    await settle("repo-group-stack");
    const team = renderer.root.findByProps({ className: "donut-legend__name" });
    await act(async () => team.parent.props.onClick());
    for (const name of ["trend", "share", "repo-group-stack"]) {
      await settle(name);
    }
    assert.equal(paths().filter(path => path.endsWith("/cost-engineering-group-share")).length, 1);
  });
});

test("Cost re-prioritizes changed scopes without reloading engineering for display-only grouping", async () => {
  await withPage(async ({ renderer, paths, settle }) => {
    for (const name of ["trend", "share", "repo-group-stack", "engineering-group-share"]) {
      await settle(name);
    }
    const engineeringCount = () => paths().filter(path => path.endsWith("/cost-engineering-group-share")).length;
    await act(async () => renderer.update(React.createElement(CostPage, {
      filters, costBreakdownGroupBy: "project",
    })));
    await settle("share");
    await settle("repo-group-stack");
    assert.equal(engineeringCount(), 1);

    await act(async () => renderer.update(React.createElement(CostPage, {
      filters: { ...filters, end_date: "2026-10-19" }, costBreakdownGroupBy: "project",
    })));
    assert.equal(engineeringCount(), 1);
    await settle("share");
    assert.equal(engineeringCount(), 1);
    await settle("trend");
    assert.equal(engineeringCount(), 1);
    await settle("repo-group-stack");
    assert.equal(engineeringCount(), 2);
    await settle("engineering-group-share");
  });
});
