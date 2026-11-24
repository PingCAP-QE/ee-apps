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

const initialFilters = {
  repo: "pingcap/tidb", branch: "master", job_name: "unit", cloud_phase: "GCP",
  start_date: "2026-11-09", end_date: "2026-11-22", issue_status: "open", granularity: "day",
};

async function withPage(check) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = (url, { signal }) => new Promise((resolve) => {
    requests.push({ url: new URL(url, "http://localhost"), signal, resolve, settled: false });
  });
  const matching = (path) => requests.filter(({ url }) => url.pathname === path);
  const respond = async (request, data) => {
    request.settled = true;
    await act(async () => request.resolve({ ok: true, json: async () => data }));
  };
  const settle = async () => {
    for (const request of requests.filter((r) => !r.settled)) {
      await respond(request, { rows: [], meta: {} });
    }
  };
  const update = async (filters) => {
    await act(async () => renderer.update(React.createElement(FlakyPage, { filters })));
  };
  const panel = () => renderer.root.findAll((node) =>
    node.props.title === "Top flaky cases" && "loading" in node.props)[0];
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(FlakyPage, { filters: initialFilters })); });
    await check({ renderer, requests, matching, respond, settle, update, panel });
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
}

const PAGE = "/api/v1/pages/flaky";
const TOP = "/api/v1/flaky/top-cases";
const BUCKET = "/api/v1/flaky/top-jobs";

function bucketPage(start, end, time) {
  return {
    bucketed_flaky_rate: {
      rows: [{ time, flaky_rate_pct: 1 }],
      meta: { start_date: start, end_date: end, effective_granularity: "week" },
    },
  };
}

test("Top cases ignores issue status/granularity changes but refetches every effective scope", async () => {
  await withPage(async ({ matching, settle, update, panel }) => {
    await settle();
    let filters = initialFilters;
    for (const change of [{ issue_status: "closed" }, { granularity: "week" }, {}]) {
      filters = { ...filters, ...change };
      await update(filters);
      await settle();
      assert.equal(matching(TOP).length, 1);
      assert.equal(panel().props.loading, false);
    }
    assert.equal(matching(PAGE).length, 3);
    for (const change of [
      { branch: "release-8.5" }, { repo: "tikv/pd" }, { job_name: "integration" },
      { cloud_phase: "TENCENT" }, { start_date: "2026-11-16" }, { end_date: "2026-11-23" },
    ]) {
      const count = matching(TOP).length;
      filters = { ...filters, ...change };
      await update(filters);
      assert.equal(matching(TOP).length, count + 1);
      const url = matching(TOP).at(-1).url;
      assert.equal(url.searchParams.get("issue_status"), null);
      assert.equal(url.searchParams.get("granularity"), null);
      for (const key of ["repo", "branch", "job_name", "cloud_phase", "start_date", "end_date"]) {
        assert.equal(url.searchParams.get(key), filters[key]);
      }
      await settle();
      assert.equal(panel().props.loading, false);
    }
  });
});

test("ignored filter changes do not abort a pending Top request or leave its panel loading", async () => {
  await withPage(async ({ matching, respond, update, panel }) => {
    const top = matching(TOP)[0];
    await respond(matching(PAGE)[0], {});
    await update({ ...initialFilters, issue_status: "closed", granularity: "week" });
    assert.equal(matching(TOP).length, 1);
    assert.equal(top.signal.aborted, false);
    await respond(top, { rows: [] });
    assert.equal(panel().props.loading, false);
  });
});

test("a new scope cannot request a bucket using retained metadata from the previous page", async () => {
  await withPage(async ({ renderer, matching, respond, update }) => {
    await respond(matching(PAGE)[0], bucketPage("2026-11-09", "2026-11-22", "2026-11-09"));
    const select = (time) => renderer.root.findAll((node) =>
      typeof node.props.onBucketSelect === "function")[0].props.onBucketSelect(time);
    await act(async () => select("2026-11-09"));
    assert.equal(matching(BUCKET).length, 1);
    const oldBucket = matching(BUCKET)[0];
    assert.equal(oldBucket.url.searchParams.get("start_date"), "2026-11-09");
    await update({ ...initialFilters, repo: "tikv/pd", start_date: "2026-11-16" });
    assert.equal(matching(BUCKET).length, 1, "do not combine old bucket dates with new repo");
    assert.equal(oldBucket.signal.aborted, true);
    await respond(matching(PAGE).at(-1), bucketPage("2026-11-16", "2026-11-22", "2026-11-16"));
    assert.equal(matching(BUCKET).length, 1, "selection must be cleared on scope change");
    await act(async () => select("2026-11-16"));
    const next = matching(BUCKET).at(-1).url;
    assert.equal(next.searchParams.get("repo"), "tikv/pd");
    assert.equal(next.searchParams.get("start_date"), "2026-11-16");
    assert.equal(next.searchParams.get("end_date"), "2026-11-22");
  });
});

test("an aborted old response cannot overwrite the new scope even if fetch ignores abort", async () => {
  await withPage(async ({ renderer, matching, respond, update, panel }) => {
    const old = matching(TOP)[0];
    await update({ ...initialFilters, branch: "release-8.5" });
    assert.equal(old.signal.aborted, true);
    const result = (case_name) => ({ rows: [{ repo: "pingcap/tidb", case_name, affected_build_count: 2, issues: [] }] });
    await respond(matching(TOP).at(-1), result("NewScopeCase"));
    assert.equal(panel().props.loading, false);
    await respond(old, result("OldScopeCase"));
    assert.equal(panel().props.loading, false);
    assert.match(JSON.stringify(renderer.toJSON()), /NewScopeCase/);
    assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /OldScopeCase/);
  });
});
