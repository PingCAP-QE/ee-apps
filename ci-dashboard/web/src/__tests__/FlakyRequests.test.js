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
  const respond = async (request, data, status = 200) => {
    request.settled = true;
    await act(async () => request.resolve({ ok: status === 200, status, statusText: "", json: async () => data }));
  };
  const settle = async () => {
    for (const request of requests) {
      if (!request.settled) await respond(request, { rows: [], meta: {} });
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

const SECTIONS = "/api/v1/pages/flaky/";
const PAGE = `${SECTIONS}chart_sections`;
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

test("a timed-out issue request neither blocks nor resets healthy Flaky panels", async () => {
  await withPage(async ({ renderer, requests, matching, respond }) => {
    const sections = () => requests.filter(({ url }) => url.pathname.startsWith(SECTIONS));
    assert.equal(sections().length, 4, "no eight-request burst");
    assert.equal(matching("/api/v1/pages/flaky").length, 0, "do not run the aggregate too");
    const delayed = matching(`${SECTIONS}issue_case_rates`)[0];
    const cards = () => renderer.root.findByProps({ className: "stats-grid" });
    const values = () => cards().findAllByProps({ className: "stat-card__value" });
    await respond(matching(`${SECTIONS}period_comparison`)[0], {
      period_comparison: { groups: [{ name: "period_a", values: { failure_like_build_count: 4, flaky_rate_pct: 25 } }] },
    });
    assert.equal(values()[0].children.join(""), "25.0%");
    const issuePanel = () => renderer.root.findAll((node) =>
      node.props.title === "Filtered-issue weekly case table" && "loading" in node.props)[0];
    assert.equal(issuePanel().props.loading, true);
    await respond(delayed, null, 504);
    assert.match(issuePanel().props.error, /504/);
    assert.equal(issuePanel().props.actions, null, "no invented zero badges on failure");
    assert.equal(values()[0].children.join(""), "25.0%");
    while (sections().some((request) => !request.settled)) {
      assert.ok(sections().filter((request) => !request.settled).length <= 4);
      await respond(sections().find((request) => !request.settled), {});
    }
    assert.equal(sections().length, 8);
    assert.equal(new Set(sections().map(({ url }) => url.pathname)).size, 8, "no replay/fallback");
  });
});

test("gateway timeouts do not free slots for more server-side work during an outage", async () => {
  await withPage(async ({ renderer, requests, respond }) => {
    const sections = () => requests.filter(({ url }) => url.pathname.startsWith(SECTIONS));
    const first = sections();
    for (const request of first) await respond(request, null, 504);
    assert.equal(sections().length, 4, "timed-out SQL may still be running behind the gateway");
    const topJobs = renderer.root.findAll((node) => node.props.title === "Top noisy jobs" && "loading" in node.props)[0];
    assert.equal(topJobs.props.loading, false, "skipped sections must not spin forever");
    assert.match(topJobs.props.error, /not requested/i);
    const cardValues = renderer.root.findAllByProps({ className: "stat-card__value" });
    assert.ok(cardValues.every((item) => item.children.join("") === "—"));
  });
});

test("late section responses neither populate nor dispatch work for an aborted scope", async () => {
  await withPage(async ({ renderer, requests, matching, respond, update }) => {
    const old = requests.filter(({ url }) => url.pathname.startsWith(SECTIONS));
    await update({ ...initialFilters, repo: "tikv/pd" });
    assert.ok(old.every(({ signal }) => signal.aborted));
    const path = `${SECTIONS}period_comparison`;
    const period = (value) => ({ period_comparison: { groups: [{ name: "period_a", values: {
      failure_like_build_count: 12, flaky_rate_pct: value,
    } }] } });
    await respond(matching(path).at(-1), period(23));
    for (const request of old) await respond(request, period(99));
    assert.equal(requests.filter(({ url }) => url.pathname.startsWith(SECTIONS) && url.searchParams.get("repo") === "pingcap/tidb").length, 4);
    assert.equal(renderer.root.findAllByProps({ className: "stat-card__value" })[0].children.join(""), "23.0%");
  });
});

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
