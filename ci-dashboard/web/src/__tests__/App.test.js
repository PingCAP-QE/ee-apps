import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React, { useMemo, useState } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createPath, MemoryRouter, parsePath, Router } from "react-router-dom";
import { createServer } from "vite";

const cachedCostUrl = "/cost?start_date=2026-07-27&end_date=2026-08-31&cost_source=gcp%3Apingcap-testing-account&granularity=week";
const ciStatusUrl = "/ci-status?start_date=2026-08-10&end_date=2026-08-10&granularity=week";
const incomingCostUrl = "/cost?start_date=2026-08-10&end_date=2026-08-10&cost_source=gcp%3Apingcap-testing-account&granularity=week";

let App;
let CostPage;
let WeeklyCostPage;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
  ({ default: CostPage } = await server.ssrLoadModule("/src/pages/CostPage.jsx"));
  ({ default: WeeklyCostPage } = await server.ssrLoadModule("/src/pages/WeeklyCostPage.jsx"));
});

after(async () => {
  await server?.close();
});

function weeklyCostReport({ items = [], listCostHistory, budgetPace, teamShare } = {}) {
  return {
    meta: {
      calendar_timezone: "UTC",
      cost_metric: "list_cost",
      purpose_schema_available: true,
    },
    last_week: { start_date: "2026-07-13", end_date: "2026-07-19" },
    previous_week: { start_date: "2026-07-06", end_date: "2026-07-12" },
    previous_month: { start_date: "2026-06-01", end_date: "2026-06-30" },
    summary: {
      last_week_cost: 0,
      previous_week_cost: 0,
      week_wow_pct: null,
      previous_month_cost: 0,
    },
    items,
    ...(listCostHistory ? { list_cost_history: listCostHistory } : {}),
    ...(budgetPace ? { budget_pace: budgetPace } : {}),
    ...(teamShare ? { team_share: teamShare } : {}),
  };
}

function weeklyCostHistory(series = []) {
  return {
    metric: "list_cost",
    start_date: "2026-05-25",
    end_date: "2026-07-19",
    weeks: [
      { start_date: "2026-05-25", end_date: "2026-05-31" },
      { start_date: "2026-06-01", end_date: "2026-06-07" },
      { start_date: "2026-06-08", end_date: "2026-06-14" },
      { start_date: "2026-06-15", end_date: "2026-06-21" },
      { start_date: "2026-06-22", end_date: "2026-06-28" },
      { start_date: "2026-06-29", end_date: "2026-07-05" },
      { start_date: "2026-07-06", end_date: "2026-07-12" },
      { start_date: "2026-07-13", end_date: "2026-07-19" },
    ],
    series,
  };
}

test("QA Cost Weekly direct route uses its fixed API URL and explains an old source schema", async () => {
  const requests = [];
  const originalFetch = globalThis.fetch;
  let renderer;

  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return {
      ok: true,
      json: async () => ({
        meta: {
          calendar_timezone: "UTC",
          cost_metric: "list_cost",
          purpose_schema_available: false,
        },
        last_week: { start_date: "2026-07-13", end_date: "2026-07-19" },
        previous_week: { start_date: "2026-07-06", end_date: "2026-07-12" },
        previous_month: { start_date: "2026-06-01", end_date: "2026-06-30" },
        summary: {
          last_week_cost: 0,
          previous_week_cost: 0,
          week_wow_pct: null,
          previous_month_cost: 0,
        },
        items: [],
      }),
    };
  };

  try {
    await act(async () => {
      renderer = TestRenderer.create(
        React.createElement(
          MemoryRouter,
          { initialEntries: ["/qa-cost-weekly?start_date=2020-01-01&repo=pingcap%2Ftidb"] },
          React.createElement(App),
        ),
      );
      await Promise.resolve();
    });

    assert.deepEqual(requests, ["/api/v1/pages/weekly-cost?include_trend=false"]);
    const rendered = JSON.stringify(renderer.toJSON());
    assert.match(rendered, /QA Cost Weekly/);
    assert.match(rendered, /QA source metadata is not deployed yet/);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost distinguishes no QA sources from configured zero-cost sources", async () => {
  const originalFetch = globalThis.fetch;
  let report = weeklyCostReport();
  let renderer;

  globalThis.fetch = async () => ({ ok: true, json: async () => report });

  async function renderReport() {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });
    return JSON.stringify(renderer.toJSON());
  }

  try {
    assert.match(await renderReport(), /No QA cost sources with a configured purpose/);
    await act(async () => renderer.unmount());

    report = weeklyCostReport({
      items: [
        {
          cost_source: "gcp:configured-zero-cost",
          vendor: "gcp",
          account_id: "configured-zero-cost",
          display_name: "configured-zero-cost",
          purpose: "Configured QA environment",
          last_week_cost: 0,
          previous_week_cost: 0,
          week_wow_pct: null,
          last_week_share_pct: null,
          previous_month_cost: 0,
        },
      ],
    });
    const rendered = await renderReport();
    const periodLabels = renderer.root
      .findAllByProps({ className: "weekly-cost__period-label" })
      .map((label) => label.children.map((part) => part.children.join("")));
    assert.match(rendered, /Configured QA environment/);
    assert.deepEqual(periodLabels, [
      ["Last week", "2026-07-13 – 2026-07-19"],
      ["Last natural month", "2026-06-01 – 2026-06-30"],
      ["Last week", "2026-07-13 – 2026-07-19"],
      ["Last natural month", "2026-06-01 – 2026-06-30"],
    ]);
    assert.doesNotMatch(rendered, /No QA cost sources with a configured purpose/);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost renders configured and unconfigured budget pace plus team shares", async () => {
  const requests = [];
  const originalFetch = globalThis.fetch;
  let renderer;
  const report = weeklyCostReport({
    budgetPace: {
      metric: "list_cost",
      period: { start_date: "2026-07-13", end_date: "2026-07-19" },
      overall: { actual_list_cost: 220, period_budget: 1050, utilization_pct: 20.95 },
      projects: [
        { key: "project:alpha", name: "Alpha", actual_list_cost: 150, period_budget: 149.59, utilization_pct: 100.27 },
        { key: "project:beta", name: "Beta", actual_list_cost: 50, period_budget: null, utilization_pct: null },
        { key: "project:gamma", name: "Gamma", actual_list_cost: 90, period_budget: 100, utilization_pct: 90 },
      ],
      team_cost: {
        metric: "list_cost",
        total_list_cost: 220,
        items: [
          { key: "team:database", name: "Database", actual_list_cost: 150, share_pct: 68.18, interactive: false },
        ],
      },
    },
    teamShare: {
      metric: "list_cost",
      total_list_cost: 220,
      root_group_available: true,
      level1: { items: [{ key: "team:database", name: "Database", value: 150, share_pct: 68.18, interactive: false }] },
      level2: { items: [{ key: "team:tidb", name: "Database / TiDB", value: 150, share_pct: 68.18, interactive: false }] },
      projects: { items: [{ key: "project:alpha", name: "Alpha", value: 150, share_pct: 68.18, interactive: false }] },
      owners: { items: [{ key: "owner:alice", name: "alice", value: 150, share_pct: 68.18, interactive: false }] },
    },
  });
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => report };
  };

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });

    const rendered = JSON.stringify(renderer.toJSON());
    assert.equal(requests.length, 2);
    assert.match(rendered, /Budget pace/);
    assert.match(rendered, /Overall budget pace/);
    assert.match(rendered, /Project test budget utilization/);
    assert.match(rendered, /Not configured/);
    assert.match(rendered, /Team test cost/);
    assert.match(rendered, /Team share/);
    assert.ok(renderer.root.findByProps({ role: "img", "aria-label": "Level 1 groups share chart" }));
    assert.ok(renderer.root.findByProps({ role: "img", "aria-label": "Level 2 teams share chart" }));
    assert.ok(renderer.root.findByProps({ role: "img", "aria-label": "Project allocation share chart" }));
    assert.ok(renderer.root.findByProps({ role: "img", "aria-label": "Owner share share chart" }));
    const overallGauge = renderer.root.findByProps({
      "aria-label": "Overall budget pace utilization gauge",
    });
    assert.equal(overallGauge.props["aria-valuenow"], 21);
    assert.match(
      overallGauge
        .findAllByType("path")
        .find((path) => path.props.className.includes("budget-gauge-fill")).props.className,
      /weekly-cost__budget-gauge-fill--healthy/,
    );
    assert.match(
      renderer.root
        .findByProps({ "aria-label": "Alpha budget utilization" })
        .findByType("span").props.className,
      /weekly-cost__budget-fill--danger/,
    );
    assert.match(
      renderer.root
        .findByProps({ "aria-label": "Gamma budget utilization" })
        .findByType("span").props.className,
      /weekly-cost__budget-fill--warning/,
    );
    assert.match(
      renderer.root
        .findByProps({ "aria-label": "Database QA team cost share" })
        .findByType("span").props.className,
      /weekly-cost__team-cost-fill/,
    );
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost switches budget pace and team share to the last natural month", async () => {
  const requests = [];
  const originalFetch = globalThis.fetch;
  let renderer;
  const weekBudgetPace = {
    metric: "list_cost",
    period: { start_date: "2026-07-13", end_date: "2026-07-19" },
    overall: { actual_list_cost: 100, period_budget: 200, utilization_pct: 50 },
    projects: [],
    team_cost: { metric: "list_cost", total_list_cost: 100, items: [] },
  };
  const monthBudgetPace = {
    metric: "list_cost",
    period: { start_date: "2026-06-01", end_date: "2026-06-30" },
    overall: { actual_list_cost: 300, period_budget: 500, utilization_pct: 60 },
    projects: [],
    team_cost: { metric: "list_cost", total_list_cost: 300, items: [] },
  };
  const teamShare = {
    metric: "list_cost",
    total_list_cost: 100,
    root_group_available: false,
    level1: { items: [] },
    level2: { items: [] },
    projects: { items: [] },
    owners: { items: [] },
  };
  const report = weeklyCostReport({ budgetPace: weekBudgetPace, teamShare });
  const monthlyAllocation = {
    period: { start_date: "2026-06-01", end_date: "2026-06-30" },
    meta: { purpose_schema_available: true },
    budget_pace: monthBudgetPace,
    team_share: { ...teamShare, total_list_cost: 300 },
  };
  let resolveMonthlyResponse;
  globalThis.fetch = (url) => {
    requests.push(String(url));
    if (String(url).includes("/allocation")) {
      return new Promise((resolve) => {
        resolveMonthlyResponse = () => resolve({ ok: true, json: async () => monthlyAllocation });
      });
    }
    return Promise.resolve({ ok: true, json: async () => report });
  };

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });
    assert.deepEqual(requests, [
      "/api/v1/pages/weekly-cost?include_trend=false",
      "/api/v1/pages/weekly-cost/trend",
    ]);

    await act(async () => {
      renderer.root
        .findAllByType("button")
        .find((button) => button.children.join("") === "Last natural month")
        .props.onClick();
      await Promise.resolve();
    });

    assert.deepEqual(requests, [
      "/api/v1/pages/weekly-cost?include_trend=false",
      "/api/v1/pages/weekly-cost/trend",
      "/api/v1/pages/weekly-cost/allocation?period=month",
    ]);
    assert.match(JSON.stringify(renderer.toJSON()), /Last natural month Jun 1 - Jun 30, 2026/);

    await act(async () => {
      resolveMonthlyResponse();
      await Promise.resolve();
    });

    const rendered = JSON.stringify(renderer.toJSON());
    assert.match(rendered, /2026-06-01 – 2026-06-30/);
    assert.deepEqual(
      renderer.root.findByProps({ className: "weekly-cost__budget-amount" }).children,
      ["$300.00", " actual", " / $500.00 budget"],
    );
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost sorts account breakdown numeric columns on demand", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () =>
      weeklyCostReport({
        items: [
          {
            cost_source: "gcp:low",
            vendor: "gcp",
            account_id: "low",
            display_name: "low",
            purpose: "QA",
            last_week_cost: 100,
            previous_week_cost: 200,
            week_wow_pct: -50,
            last_week_share_pct: 25,
            previous_month_cost: 300,
          },
          {
            cost_source: "aws:high",
            vendor: "aws",
            account_id: "high",
            display_name: "high",
            purpose: "QA",
            last_week_cost: 300,
            previous_week_cost: 100,
            week_wow_pct: 200,
            last_week_share_pct: 75,
            previous_month_cost: 200,
          },
        ],
      }),
  });

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });

    const rowsByClassName = (className) =>
      renderer.root
        .findByType("tbody")
        .findAllByType("tr")
        .filter((row) => row.props.className === className);
    const accountNames = () =>
      rowsByClassName("weekly-cost__account-row").map((row) => row.findByType("strong").children.join(""));
    assert.deepEqual(
      rowsByClassName("weekly-cost__vendor-total").map((row) => row.findByType("strong").children.join("")),
      ["AWS Sum", "GCP Sum", "AZURE Sum"],
    );
    assert.deepEqual(accountNames(), ["GCP · low", "AWS · high"]);

    await act(async () => {
      renderer.root
        .findByProps({ "aria-label": "Sort Last week descending" })
        .props.onClick();
    });
    assert.deepEqual(accountNames(), ["AWS · high", "GCP · low"]);

    await act(async () => {
      renderer.root
        .findByProps({ "aria-label": "Sort Last week ascending" })
        .props.onClick();
    });
    assert.deepEqual(accountNames(), ["GCP · low", "AWS · high"]);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost flags only row WoW values above 30%", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () =>
      weeklyCostReport({
        items: [
          {
            cost_source: "gcp:at-threshold",
            vendor: "gcp",
            account_id: "at-threshold",
            display_name: "at-threshold",
            purpose: "QA",
            last_week_cost: 130,
            previous_week_cost: 100,
            week_wow_pct: 30,
            last_week_share_pct: 33.33,
            previous_month_cost: 100,
          },
          {
            cost_source: "gcp:above-threshold",
            vendor: "gcp",
            account_id: "above-threshold",
            display_name: "above-threshold",
            purpose: "QA",
            last_week_cost: 130.01,
            previous_week_cost: 100,
            week_wow_pct: 30.01,
            last_week_share_pct: 33.33,
            previous_month_cost: 100,
          },
          {
            cost_source: "gcp:no-comparison",
            vendor: "gcp",
            account_id: "no-comparison",
            display_name: "no-comparison",
            purpose: "QA",
            last_week_cost: 0,
            previous_week_cost: 0,
            week_wow_pct: null,
            last_week_share_pct: 33.34,
            previous_month_cost: 0,
          },
        ],
      }),
  });

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });

    const wowCells = renderer.root
      .findByType("tbody")
      .findAllByType("tr")
      .filter((row) => row.props.className === "weekly-cost__account-row")
      .map((row) => row.findAllByType("td")[2]);

    assert.ok(renderer.root.findByProps({ "aria-label": "Sort WoW descending" }));
    assert.deepEqual(
      wowCells.map((cell) => cell.children.join("")),
      ["30.0%", "30.0%", "—"],
    );
    assert.deepEqual(
      wowCells.map((cell) => cell.props.className),
      [
        "weekly-cost__number weekly-cost__wow",
        "weekly-cost__number weekly-cost__wow weekly-cost__wow--alert",
        "weekly-cost__number weekly-cost__wow",
      ],
    );
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("weekly cost renders and focuses list-cost history without refetching", async () => {
  const requests = [];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  let renderer;
  const points = (first, second) => [
    ["2026-05-25", first],
    ["2026-06-01", 0],
    ["2026-06-08", 0],
    ["2026-06-15", 0],
    ["2026-06-22", 0],
    ["2026-06-29", 0],
    ["2026-07-06", 0],
    ["2026-07-13", second],
  ].map(([week_start, list_cost]) => ({ week_start, list_cost }));
  const report = weeklyCostReport({
    items: [
      {
        cost_source: "gcp:alpha",
        vendor: "gcp",
        account_id: "alpha",
        display_name: "alpha",
        purpose: "A long configured QA purpose",
        last_week_cost: 10,
        previous_week_cost: 0,
        week_wow_pct: null,
        last_week_share_pct: 100,
        previous_month_cost: 10,
      },
    ],
    listCostHistory: weeklyCostHistory([
      {
        cost_source: "gcp:alpha",
        vendor: "gcp",
        account_id: "alpha",
        display_name: "alpha",
        purpose: "A long configured QA purpose",
        total_list_cost: 30,
        points: points(10, 20),
      },
      {
        cost_source: "aws:beta",
        vendor: "aws",
        account_id: "beta",
        display_name: "beta",
        purpose: "Another QA purpose",
        total_list_cost: 15,
        points: points(5, 10),
      },
      ...Array.from({ length: 10 }, (_value, index) => ({
        cost_source: `gcp:filler-${index}`,
        vendor: "gcp",
        account_id: `filler-${index}`,
        display_name: `filler-${index}`,
        purpose: "Additional QA purpose",
        total_list_cost: 2,
        points: points(1, 1),
      })),
      {
        cost_source: "gcp:overflow",
        vendor: "gcp",
        account_id: "overflow",
        display_name: "overflow",
        purpose: "Additional QA purpose",
        total_list_cost: 2,
        points: points(1, 1),
      },
    ]),
  });

  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => report };
  };
  globalThis.window = {
    setTimeout(callback) {
      callback();
      return 1;
    },
    clearTimeout() {},
  };

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(WeeklyCostPage));
      await Promise.resolve();
    });

    assert.equal(requests.length, 2);
    assert.ok(renderer.root.findByProps({ role: "img", "aria-label": "Trend chart" }));
    assert.ok(
      renderer.root.findAllByType("h3").some((heading) => heading.children.join("") === "Cost trend"),
    );
    const costTrendLabels = renderer.root
      .findAll((node) => node.props.className === "chart-axis-label chart-axis-label--bottom");
    assert.deepEqual(
      costTrendLabels.map((label) => label.children.join("")),
      [
        "2026-05-25",
        "2026-06-01",
        "2026-06-08",
        "2026-06-15",
        "2026-06-22",
        "2026-06-29",
        "2026-07-06",
        "2026-07-13",
      ],
    );
    assert.ok(
      costTrendLabels.every(
        (label) =>
          label.props.style.fontSize === "9px" &&
          label.props.textAnchor === "end" &&
          label.props.transform === `rotate(45 ${label.props.x} ${label.props.y})`,
      ),
    );
    const overflowColor = "hsl(280 68% 38%)";
    assert.equal(renderer.root.findAllByProps({ className: "chart-legend" }).length, 0);
    assert.deepEqual(accountSelectorLabels(renderer).slice(0, 3), ["All", "GCP · alpha", "AWS · beta"]);
    assert.equal(accountSelectorLabels(renderer).at(-1), "GCP · overflow");
    assert.deepEqual(accountSelectorColors(renderer).slice(0, 2), ["#0072b2", "#d55e00"]);
    assert.equal(accountSelectorColors(renderer).at(-1), overflowColor);
    assert.ok(chartBarColors(renderer).includes(overflowColor));

    const lastHitArea = renderer.root.findAllByProps({ className: "chart-hit-area" }).at(-1);
    await act(async () => {
      lastHitArea.props.onMouseEnter();
    });
    assert.equal(
      renderer.root.findByProps({ className: "chart-tooltip__title" }).children.join(""),
      "2026-07-13 – 2026-07-19",
    );
    await act(async () => {
      lastHitArea.props.onMouseLeave();
    });

    await act(async () => {
      renderer.root
        .findAllByType("button")
        .find((button) => button.props["aria-label"] === "Show GCP · overflow")
        .props.onClick();
    });
    assert.equal(requests.length, 2);
    assert.ok(
      renderer.root
        .findAllByType("button")
        .find((button) => button.props["aria-label"] === "Show GCP · overflow")
        .props["aria-pressed"],
    );
    assert.ok(chartBarColors(renderer).every((color) => color === overflowColor));
    assert.equal(accountSelectorColors(renderer).at(-1), overflowColor);

    await act(async () => {
      renderer.root
        .findAllByType("button")
        .find((button) => button.props["aria-label"] === "Show all accounts")
        .props.onClick();
    });
    assert.equal(requests.length, 2);
    assert.ok(
      renderer.root
        .findAllByType("button")
        .find((button) => button.props["aria-label"] === "Show all accounts")
        .props["aria-pressed"],
    );
    assert.ok(chartBarColors(renderer).includes("#0072b2"));
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});

function accountSelectorLabels(renderer) {
  return renderer.root
    .findByProps({ className: "dimension-selector weekly-cost__account-selector" })
    .findAllByType("button")
    .map((button) => button.findAllByType("span").at(-1).children.join(""));
}

function accountSelectorColors(renderer) {
  return renderer.root
    .findAllByProps({ className: "weekly-cost__account-selector-dot" })
    .map((dot) => dot.props.style.backgroundColor);
}

function chartBarColors(renderer) {
  return renderer.root
    .findAllByType("rect")
    .filter((rect) => rect.props.opacity === "0.78")
    .map((rect) => rect.props.fill);
}

test("incoming route filters win over cached filters without URL or request oscillation", async () => {
  const requests = [];
  const replacements = [];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  let currentLocation;
  let navigate;
  let renderer;
  let locationKey = 0;

  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => ({}) };
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

  function TestRouter() {
    const [location, setLocation] = useState(() => toLocation(cachedCostUrl));
    const navigator = useMemo(() => {
      function updateLocation(to, state, replace) {
        const nextLocation = toLocation(to, state);
        if (replace) {
          replacements.push(createPath(nextLocation));
          if (replacements.length > 5) {
            // Keep an oscillation regression from hanging the test runner.
            return;
          }
        }
        setLocation(nextLocation);
      }

      return {
        createHref: (to) => createPath(to),
        encodeLocation: (to) => to,
        go: () => {},
        push: (to, state) => updateLocation(to, state, false),
        replace: (to, state) => updateLocation(to, state, true),
      };
    }, []);

    currentLocation = location;
    navigate = navigator.push;
    return React.createElement(
      Router,
      { location, navigator },
      React.createElement(App),
    );
  }

  try {
    await act(async () => {
      renderer = TestRenderer.create(React.createElement(TestRouter));
    });
    await act(async () => navigate(ciStatusUrl));

    requests.length = 0;
    replacements.length = 0;
    await act(async () => navigate(incomingCostUrl));

    const costTrendRequests = requests.filter((url) =>
      url.startsWith("/api/v1/pages/cost-trend?"),
    );
    assert.equal(createPath(currentLocation), incomingCostUrl);
    assert.equal(replacements.length, 0);
    assert.equal(costTrendRequests.length, 1);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }

  function toLocation(to, state = null) {
    const parsed = typeof to === "string" ? parsePath(to) : to;
    return {
      pathname: parsed.pathname || "/",
      search: parsed.search || "",
      hash: parsed.hash || "",
      state,
      key: String(locationKey += 1),
    };
  }
});

test("resource breakdown renders identifiers and loads the next page", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let returnPending = false;
  let renderer;

  globalThis.fetch = async (url) => {
    const request = String(url);
    requests.push(request);
    if (request.startsWith("/api/v1/pages/cost-share")) {
      return {
        ok: true,
        json: async () => ({
          items: [{ name: "alice", value: 5, share_pct: 100 }],
          meta: { total_list_cost: 5 },
        }),
      };
    }
    if (request.startsWith("/api/v1/pages/cost-unmatched-resources")) {
      const secondPage = request.includes("cursor=next-page");
      return {
        ok: true,
        json: async () => ({
          items: returnPending ? [] : [
            secondPage
              ? {
                  resource_key: "bucket",
                  resource_id: null,
                  resource_name: "billing-bucket",
                  service_name: "AmazonS3",
                  labels: "Name=billing-bucket",
                  list_cost: 2,
                  usage_seconds: null,
                }
              : {
                  resource_key: "instance",
                  resource_id: "i-0123456789abcdef0",
                  resource_name: "i-0123456789abcdef0",
                  service_name: "AmazonEC2",
                  labels: "Name=runner",
                  list_cost: 3,
                  usage_seconds: 3600,
                },
          ],
          meta: {
            services: [],
            pending_dates: returnPending ? ["2026-08-10"] : [],
            next_cursor: returnPending || secondPage ? null : "next-page",
          },
        }),
      };
    }
    return {
      ok: true,
      json: async () => ({ items: [], meta: { summary: {} }, summary: {} }),
    };
  };

  try {
    await act(async () => {
      renderer = TestRenderer.create(
        React.createElement(CostPage, {
          filters: {
            start_date: "2026-08-10",
            end_date: "2026-08-10",
            granularity: "week",
            cost_source: "gcp:pingcap-testing-account",
          },
        }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    assert.ok(!requests.some((request) => request.startsWith("/api/v1/pages/cost-budget-pace?")));
    assert.ok(!requests.some((request) => request.startsWith("/api/v1/pages/cost-weekly-overview")));
    assert.ok(!requests.some((request) => request.startsWith("/api/v1/pages/cost-unattached-block-volumes")));
    assert.doesNotMatch(
      JSON.stringify(renderer.toJSON()),
      /Fiscal budget|Budget pace|Weekly overview|Unattached Block Volumes/,
    );

    const ownerLabel = renderer.root
      .findAllByProps({ className: "donut-legend__name" })
      .find((node) => node.children.join("") === "alice");
    await act(async () => {
      ownerLabel.parent.props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });

    const headers = renderer.root
      .findByType("thead")
      .findAllByType("th")
      .map((header) => header.children.join(""));
    assert.deepEqual(headers, ["Resource ID", "Name", "Service", "List cost", "Duration", "Labels"]);
    assert.match(JSON.stringify(renderer.toJSON()), /i-0123456789abcdef0/);

    await act(async () => {
      renderer.root
        .findAllByType("button")
        .find((button) => button.children.join("") === "Load more")
        .props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });

    assert.match(JSON.stringify(renderer.toJSON()), /billing-bucket/);
    assert.ok(requests.some((request) => request.includes("cursor=next-page")));

    returnPending = true;
    await act(async () => {
      renderer.root
        .findAllByType("button")
        .find((button) => button.children.join("") === "Duration")
        .props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });
    assert.ok(
      renderer.root
        .findAllByProps({ className: "empty-state" })
        .some((node) => node.children.some(
          (child) => typeof child === "string" && child.includes("Resource data is unavailable for"),
        )),
    );
    assert.equal(
      renderer.root.findByType("a").children.join(""),
      "Refresh the resource-serving projection",
    );
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});
