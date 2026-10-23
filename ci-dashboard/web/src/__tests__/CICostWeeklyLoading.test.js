import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createServer } from "vite";

let CICostWeeklyPage;
let Panel;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: CICostWeeklyPage } = await server.ssrLoadModule("/src/pages/CICostWeeklyPage.jsx"));
  ({ Panel } = await server.ssrLoadModule("/src/components/charts.jsx"));
});

after(async () => {
  await server?.close();
});

for (const failedSection of [null, "overview", "share", "categories"]) {
  test(`CI weekly cost progressively loads independent panels (failure: ${failedSection})`, async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    const pending = new Map();
    const signals = [];
    let renderer;
    globalThis.fetch = (url, { signal }) => new Promise((resolve) => {
      requests.push(String(url));
      signals.push(signal);
      pending.set(new URL(String(url), "http://localhost").searchParams.get("section"), resolve);
    });
    const overview = {
      accounts: [{
        vendor: "gcp",
        cost_source: "gcp:pingcap-testing-account",
        cost_basis: "list_cost",
        last_complete_week: { actual_cost: 123, period_budget: 200, utilization_pct: 61.5 },
      }],
    };
    const settle = async (section, data) => {
      await act(async () => {
        pending.get(section)({
          ok: section !== failedSection,
          status: 503,
          statusText: "Service Unavailable",
          json: async () => data,
        });
      });
    };
    const panels = () => renderer.root.findAllByType(Panel);
    const assertLoading = (expected) => {
      assert.deepEqual(panels().map((panel) => Boolean(panel.props.loading)), expected);
    };
    try {
      await act(async () => {
        renderer = TestRenderer.create(React.createElement(CICostWeeklyPage));
      });
      assert.deepEqual(requests, ["/api/v1/pages/ci-weekly-cost?section=overview"]);
      assertLoading([true, true, true, true]);

      await settle("overview", overview);
      assert.deepEqual(requests, [
        "/api/v1/pages/ci-weekly-cost?section=overview",
        "/api/v1/pages/ci-weekly-cost?section=share",
      ]);
      assertLoading([false, false, true, true]);
      if (failedSection !== "overview") {
        assert.match(JSON.stringify(renderer.toJSON()), /\$123\.00/);
      }

      await settle("share", {
        last_week_cost_share: {
          total_list_cost: 123,
          teams: { items: [{ key: "tidb", name: "TiDB", value: 123, share_pct: 100 }] },
        },
      });
      assert.equal(requests.at(-1), "/api/v1/pages/ci-weekly-cost?section=categories");
      assertLoading([false, false, false, true]);
      assert.equal(Boolean(panels()[0].props.error), failedSection === "overview");
      assert.equal(Boolean(panels()[2].props.error), failedSection === "share");

      await settle("categories", { last_week_category_share: { accounts: [] } });
      assertLoading([false, false, false, false]);
      assert.equal(Boolean(panels()[3].props.error), failedSection === "categories");
      if (failedSection !== "overview") {
        assert.match(JSON.stringify(renderer.toJSON()), /\$123\.00/);
      }
      assert.equal(requests.length, 3);
    } finally {
      await act(async () => renderer?.unmount());
      assert.ok(signals.every((signal) => signal.aborted));
      globalThis.fetch = originalFetch;
    }
  });
}
