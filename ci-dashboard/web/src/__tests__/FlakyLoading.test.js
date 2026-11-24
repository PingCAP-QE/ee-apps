import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createServer } from "vite";

let FlakyPage;
let server;
before(async () => {
  server = await createServer({
    root: process.cwd(), appType: "custom", logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: FlakyPage } = await server.ssrLoadModule("/src/pages/FlakyPage.jsx"));
});
after(async () => server?.close());

const filters = {
  repo: "pingcap/tidb", branch: "release-nextgen-202603", issue_status: "closed",
  start_date: "2026-11-16", end_date: "2026-11-22", granularity: "day",
};
const summary = {
  failure_like_build_count: 108, flaky_build_count: 17, flaky_rate_pct: 15.74,
  retry_loop_build_count: 2, retry_loop_rate_pct: 1.85,
  noisy_build_count: 19, noisy_rate_pct: 17.59,
  total_pr_count: 17, affected_pr_count: 3, affected_pr_rate_pct: 17.65,
};

async function withPage(check) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = (url, { signal }) => new Promise((resolve) => {
    requests.push({ url: String(url), signal, resolve, settled: false });
  });
  const settle = async (values = summary, failed = false, sections = {}) => {
    const request = requests.findLast(({ url, settled }) => !settled
      && new URL(url, "http://localhost").pathname === "/api/v1/pages/flaky");
    assert.ok(request);
    request.settled = true;
    await act(async () => request.resolve({
      ok: !failed, status: 504, statusText: "Gateway Timeout",
      json: async () => ({ ...sections, period_comparison: { groups: [{ name: "period_a", values }] } }),
    }));
  };
  const cards = () => renderer.root.findByProps({ className: "stats-grid" });
  const values = () => cards().findAllByProps({ className: "stat-card__value" })
    .map((card) => card.children.join(""));
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(FlakyPage, { filters })); });
    await check({ renderer, settle, cards, values, requests });
  } finally {
    await act(async () => renderer?.unmount());
    assert.ok(requests.every(({ signal }) => signal.aborted));
    globalThis.fetch = originalFetch;
  }
}

test("Flaky cards show loading placeholders, then the real rates", async () => {
  await withPage(async ({ settle, cards, values }) => {
    assert.deepEqual(values(), ["—", "—", "—", "—"]);
    assert.ok(cards().findAllByType("span").some((node) => node.children.join("").includes("Loading")));
    await settle();
    assert.deepEqual(values(), ["15.7%", "1.9%", "17.6%", "17.6%"]);
  });
});

test("Flaky cards never label a prior branch's metrics as the new scope", async () => {
  await withPage(async ({ renderer, settle, values }) => {
    await settle();
    await act(async () => renderer.update(React.createElement(FlakyPage, {
      filters: { ...filters, branch: "release-nextgen-202609" },
    })));
    assert.deepEqual(values(), ["—", "—", "—", "—"]);
    await settle({ ...summary, flaky_build_count: 2, failure_like_build_count: 83, flaky_rate_pct: 2.41 });
    assert.equal(values()[0], "2.4%");
  });
});

test("Flaky cards display request errors rather than zero or stale rates", async () => {
  await withPage(async ({ renderer, settle, cards, values }) => {
    await settle();
    await act(async () => renderer.update(React.createElement(FlakyPage, {
      filters: { ...filters, issue_status: "" },
    })));
    await settle(summary, true);
    assert.deepEqual(values(), ["—", "—", "—", "—"]);
    assert.ok(cards().findAllByType("span").some((node) => node.children.join("").includes("504")));
  });
});

test("bucket job rankings hide the prior scope before the new request effect runs", async () => {
  await withPage(async ({ renderer, settle, requests }) => {
    const weeks = ["2026-11-09", "2026-11-16"];
    await settle(summary, false, {
      bucketed_flaky_rate: {
        rows: weeks.map((time) => ({ time, flaky_rate_pct: 1 })),
        meta: { start_date: weeks[0], end_date: "2026-11-22", effective_granularity: "week" },
      },
    });
    const selectBucket = (time) => renderer.root.findAll((node) =>
      typeof node.props.onBucketSelect === "function")[0].props.onBucketSelect(time);
    const rankingPanel = () => renderer.root.findAll((node) =>
      node.props.title === "Top noisy jobs" && "loading" in node.props)[0];
    await act(async () => selectBucket(weeks[0]));
    const request = requests.findLast(({ url }) => url.includes("/flaky/top-jobs?"));
    await act(async () => request.resolve({
      ok: true, json: async () => ({ items: [{ name: "Old bucket job", noisy_rate_pct: 12 }] }),
    }));
    assert.equal(rankingPanel().props.loading, false);
    // Inspect the synchronous commit, before useApiData's effect flips loading.
    selectBucket(weeks[1]);
    assert.equal(rankingPanel().props.loading, true);
    await act(async () => {});
  });
});

test("Flaky distinguishes a measured zero from a missing denominator", async () => {
  await withPage(async ({ settle, values }) => {
    const zeros = Object.fromEntries(Object.keys(summary).map((key) => [key, 0]));
    await settle({ ...zeros, failure_like_build_count: 17, total_pr_count: 5 });
    assert.deepEqual(values(), ["0.0%", "0.0%", "0.0%", "0.0%"]);
  });
  await withPage(async ({ settle, values }) => {
    await settle(Object.fromEntries(Object.keys(summary).map((key) => [key, 0])));
    assert.deepEqual(values(), ["N/A", "N/A", "N/A", "N/A"]);
  });
});
