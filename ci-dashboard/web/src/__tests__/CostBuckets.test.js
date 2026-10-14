import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { MemoryRouter } from "react-router-dom";
import { createServer } from "vite";

let App;
let CostFilterControls;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
  ({ default: CostFilterControls } = await server.ssrLoadModule("/src/components/CostFilterControls.jsx"));
});

after(async () => server?.close());

function bucketSelect(renderer) {
  return renderer.root.findAllByType("select").find((select) => (
    select.findAllByType("option").some((option) => option.props.value === "day")
  ));
}

function costRequests(requests) {
  return requests.map((url) => new URL(url, "http://localhost")).filter((url) => (
    url.pathname.startsWith("/api/v1/pages/cost-") && url.searchParams.has("granularity")
  ));
}

async function withCostApp(search, run) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const requests = [];
  let renderer;
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => ({ items: [] }) };
  };
  globalThis.window = {
    scrollY: 0,
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    setTimeout,
    clearTimeout,
  };
  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(
        MemoryRouter,
        { initialEntries: [`/cost?${search}`] },
        React.createElement(App),
      ));
    });
    await run(renderer, requests);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
}

test("Cost End has an accessible T+3 hint and the same latest date as its calendar limit", async (context) => {
  context.mock.timers.enable({ apis: ["Date"], now: Date.UTC(2026, 9, 14, 12) });
  let renderer;
  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(CostFilterControls, {
        filters: { start_date: "2026-10-01", end_date: "2026-10-11", granularity: "day" },
        onFilterChange: () => {},
      }));
    });
    const endInput = renderer.root.findAllByType("input").filter((input) => input.props.type === "date")[1];
    assert.equal(endInput.props.max, "2026-10-11");
    const hint = renderer.root.findByProps({ "aria-label": "Cost data availability" });
    const tooltip = renderer.root.findByProps({ id: hint.props["aria-describedby"] });
    assert.equal(tooltip.props.role, "tooltip");
    assert.match(tooltip.children.join(""), /T\+3.*2026-10-11.*UTC/);
    assert.ok(renderer.root.findAllByType("label").some((label) => label.props.htmlFor === endInput.props.id));
  } finally {
    await act(async () => renderer?.unmount());
  }
});

test("Cost End bookmarks and typed dates cannot request newer than T+3", async (context) => {
  context.mock.timers.enable({ apis: ["Date"], now: Date.UTC(2026, 9, 14, 12) });
  await withCostApp("start_date=2026-10-01&end_date=2026-10-14&granularity=day", async (renderer, requests) => {
    const endInput = renderer.root.findAllByType("input").filter((input) => input.props.type === "date")[1];
    assert.equal(endInput.props.value, "2026-10-11");
    assert.ok(costRequests(requests).length >= 5);
    assert.ok(costRequests(requests).every((url) => url.searchParams.get("end_date") === "2026-10-11"));
    await act(async () => endInput.props.onChange({ target: { value: "2026-10-15" } }));
    assert.equal(endInput.props.value, "2026-10-11");
    assert.ok(costRequests(requests).every((url) => url.searchParams.get("end_date") === "2026-10-11"));
  });
});

test("Cost offers native Day/Week/Month and explains disabled choices", async () => {
  const changes = [];
  const controls = (end_date) => React.createElement(CostFilterControls, {
    filters: { start_date: "2026-07-01", end_date, granularity: "month" },
    onFilterChange: (...args) => changes.push(args),
  });
  let renderer;
  try {
    await act(async () => { renderer = TestRenderer.create(controls("2026-07-14")); });
    const options = () => bucketSelect(renderer).findAllByType("option");
    assert.deepEqual(options().map((option) => option.children.join("")), ["Day", "Week", "Month"]);
    assert.deepEqual(options().map((option) => option.props.disabled), [false, false, false]);
    await act(async () => bucketSelect(renderer).props.onChange({ target: { value: "day" } }));
    assert.deepEqual(changes, [["granularity", "day"]]);

    await act(async () => renderer.update(controls("2026-07-15")));
    assert.deepEqual(options().map((option) => option.props.disabled), [true, false, false]);
    assert.match(options()[0].props.title, /14 days, including both dates/);
    assert.equal(bucketSelect(renderer).props["aria-describedby"], "cost-bucket-limits");
    assert.match(renderer.root.findByProps({ id: "cost-bucket-limits" }).children.join(""), /Day requires.*14 days/);

    await act(async () => renderer.update(controls("2026-08-31")));
    assert.deepEqual(options().map((option) => option.props.disabled), [true, false, false]);
    await act(async () => renderer.update(controls("2026-09-01")));
    assert.deepEqual(options().map((option) => option.props.disabled), [true, true, false]);
    const reason = renderer.root.findByProps({ id: "cost-bucket-limits" }).children.join("");
    assert.match(reason, /Day requires.*14 days/);
    assert.match(reason, /Week requires.*before 2026-09-01.*2 calendar months/);

    await act(async () => renderer.update(controls("")));
    assert.deepEqual(options().map((option) => option.props.disabled), [true, true, false]);
    assert.match(renderer.root.findByProps({ id: "cost-bucket-limits" }).children.join(""), /valid start and end dates/);
  } finally {
    await act(async () => renderer?.unmount());
  }
});

test("Cost App Day requests stay Day through all panels and resource loading", async () => {
  await withCostApp("start_date=2026-07-01&end_date=2026-07-14&granularity=day", async (renderer, requests) => {
    assert.equal(bucketSelect(renderer).props.value, "day");
    const rangedRequests = costRequests(requests);
    assert.deepEqual(new Set(rangedRequests.map((url) => url.pathname)), new Set([
      "/api/v1/pages/cost-filter-values",
      "/api/v1/pages/cost-trend",
      "/api/v1/pages/cost-share",
      "/api/v1/pages/cost-repo-group-stack",
      "/api/v1/pages/cost-engineering-group-share",
    ]));
    assert.ok(rangedRequests.every((url) => url.searchParams.get("granularity") === "day"));
    assert.match(JSON.stringify(renderer.toJSON()), /day buckets/);
    await act(async () => renderer.root.findAllByType("button").find((button) => (
      button.children.join("") === "Load resource breakdown"
    )).props.onClick());
    const resourceRequests = costRequests(requests).filter((url) => url.pathname === "/api/v1/pages/cost-unmatched-resources");
    assert.ok(resourceRequests.length > 0);
    assert.ok(resourceRequests.every((url) => url.searchParams.get("granularity") === "day"));
  });
});

test("Cost illegal bookmarked buckets issue only legal requests from the initial render", async () => {
  for (const [granularity, end_date, expected] of [
    ["day", "2026-07-15", "week"],
    ["day", "2026-09-01", "month"],
    ["week", "2026-08-31", "week"],
    ["week", "2026-09-01", "month"],
  ]) {
    await withCostApp(`start_date=2026-07-01&end_date=${end_date}&granularity=${granularity}`, async (renderer, requests) => {
      const rangedRequests = costRequests(requests);
      assert.ok(rangedRequests.length >= 5);
      assert.equal(bucketSelect(renderer).props.value, expected);
      assert.ok(rangedRequests.every((url) => url.searchParams.get("granularity") === expected), requests.join("\n"));
      assert.ok(rangedRequests.every((url) => url.searchParams.get("end_date") === end_date));
    });
  }
});

test("Cost date changes synchronously coarsen Day to Week to Month without auto-refining", async () => {
  await withCostApp("start_date=2026-07-01&end_date=2026-07-14&granularity=day", async (renderer, requests) => {
    const changeDate = async (index, value, expected) => {
      const previousRequestCount = requests.length;
      await act(async () => renderer.root.findAllByType("input").filter((input) => (
        input.props.type === "date"
      ))[index].props.onChange({ target: { value } }));
      assert.equal(bucketSelect(renderer).props.value, expected);
      const newRequests = costRequests(requests.slice(previousRequestCount));
      assert.ok(newRequests.length >= 5);
      assert.ok(newRequests.every((url) => url.searchParams.get("granularity") === expected));
    };
    await changeDate(1, "2026-07-15", "week");
    await changeDate(1, "2026-07-14", "week");
    await changeDate(1, "2026-09-01", "month");
    await changeDate(0, "2026-08-31", "month");
  });
});
