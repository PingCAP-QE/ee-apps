import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { MemoryRouter } from "react-router-dom";
import { createServer } from "vite";

let server;
let cacheDir;
let UtilizationPage;
let App;
let utilizationPercent;

before(async () => {
  cacheDir = await mkdtemp("/tmp/ci-utilization-vite-");
  server = await createServer({
    root: process.cwd(), appType: "custom", logLevel: "silent", server: { middlewareMode: true },
    configLoader: "runner", cacheDir,
  });
  ({ default: UtilizationPage, utilizationPercent } = await server.ssrLoadModule("/src/pages/UtilizationPage.jsx"));
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
});

after(async () => {
  await server?.close();
  if (cacheDir) await rm(cacheDir, { recursive: true, force: true });
});

const readyStatus = {
  version: 3, available: true, configured: true, collection_enabled: true, healthy: false,
  latest_machine_usage_date: "2026-11-23", pod_collection_enabled: true, workload_history_available: false,
  maintenance_stale: false, paused: true, pause_reasons: ["budget"], active_contexts: 12,
  task_backlog: { pending: 2, running: 1, retry: 3 },
  label_keys: ["repo", "job", "test_project", "test_plan", "environment", "project", "service"],
  machine_window: { start_date: "2025-11-25", end_date: "2026-11-24" },
  default_window: { start_date: "2026-11-16", end_date: "2026-11-24" },
  workload_window: { start_date: "2026-11-16", end_date: "2026-11-24" },
};
const missingMachineMetric = { avg: null, peak: null, sample_minutes: 0 };
const machineData = {
  available: true, has_more: true, kind: "machine", items: [{
    usage_date: "2026-11-23", machine_id: "np-actual-machine", region: "ap-beijing",
    cluster_id: "cls-example", resource_class: "tke_native",
    metrics: { cpu: { avg: 0, peak: 0, sample_minutes: 17 }, memory: missingMachineMetric },
  }],
};
const workloadData = {
  available: true, has_more: false, kind: "workload", group_by: null,
  start_date: "2026-11-16", end_date: "2026-11-24",
  items: [{ group_value: null, metrics: { cpu: { avg: 55.5, coverage: 0.53 }, memory: { avg: null, coverage: 0 } } }],
};

function text(node) {
  return typeof node === "string" ? node : node.children.map(text).join("");
}

function field(renderer, label) {
  return renderer.root.findAllByType("label").find((node) => node.findByType("span").children.join("") === label)
    .findAll((node) => node.type === "input" || node.type === "select")[0];
}

function response(url, machine = machineData, workload = workloadData, status = readyStatus) {
  const parsed = new URL(url, "http://localhost");
  const data = parsed.pathname.endsWith("/status") ? status
    : parsed.searchParams.get("kind") === "machine" ? machine : workload;
  return { ok: true, json: async () => data };
}

for (const reason of ["disabled", "missing_schema", "database_unavailable", "unconfigured", "legacy_backend"]) {
  test(`${reason} is informative, queries only status, and preserves other App routes`, async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    let renderer;
    globalThis.fetch = async (url) => {
      requests.push(String(url));
      const status = reason === "legacy_backend" ? { ...readyStatus, version: 2 } : {
        version: 3, available: reason === "unconfigured", configured: false, reason,
        pod_collection_enabled: true, workload_history_available: false,
      };
      return { ok: true, json: async () => status };
    };
    try {
      await act(async () => {
        renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: ["/utilization"] }, React.createElement(App)));
      });
      assert.deepEqual(requests, ["/api/v1/utilization/status"]);
      const content = JSON.stringify(renderer.toJSON());
      assert.match(content, /Resource Utilization/);
      assert.match(content, /Tencent machine history and workload labels/);
      assert.doesNotMatch(content, /account weekly summaries/i);
      assert.match(content, /Tencent CICD/);
      assert.doesNotMatch(content, /Mean 0\.0%|Workload business labels/);
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

test("V3 machine observations and workload ratios stay separate while filters and paging are preserved", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return response(url, machineData, { ...workloadData, partial: true });
  };
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.equal(requests.length, 3);
    assert.ok(requests.some((url) => url.includes("kind=machine") && url.includes("start_date=2026-11-16")));
    assert.ok(requests.some((url) => url.includes("kind=workload")));
    assert.ok(!requests.some((url) => url.includes("/weekly")));
    const content = JSON.stringify(renderer.toJSON());
    for (const text of ["np-actual-machine", "cls-example", "ap-beijing", "tke_native", "Collection paused:", "Maintenance: ", "Health: ", "Task backlog:", "Active Pod contexts:", "machines and Pods", "largest observed one-minute mean"]) {
      assert.ok(content.includes(text), text);
    }
    assert.match(content, /Latest saved machine observation \(Beijing date\): .*2026-11-23.*This is not a measure of data completeness/);
    assert.doesNotMatch(content, /cutoff|refresh window|absolute usage|expected capacity|historical spec|account weekly|core·s|byte·s/i);
    assert.match(content, /do not measure machine utilization or allocate costs/);
    assert.match(content, /Partial data:.*Coverage shown is for settled windows only/);
    const machineCells = renderer.root.findByProps({ "aria-label": "Machine utilization" }).findAllByType("td");
    assert.equal(text(machineCells[0]), "2026-11-23");
    assert.equal(text(machineCells[3]), "Mean 0.0% · Peak 0.0% · Sample minutes: 17");
    assert.equal(text(machineCells[4]), "Mean N/A · Peak N/A · Sample minutes: 0");
    const workloadCells = renderer.root.findByProps({ "aria-label": "Workload utilization" }).findAllByType("td");
    assert.equal(workloadCells.length, 3);
    assert.equal(text(workloadCells[1]), "Mean 55.5% · Coverage 53.0%");
    assert.equal(text(workloadCells[2]), "Mean N/A · Coverage 0.0%");
    assert.equal(renderer.root.findAllByType("svg").length, 0, "no weekly chart");
    assert.equal(utilizationPercent(null), "N/A");
    assert.equal(utilizationPercent(0), "0.0%");
    assert.equal(utilizationPercent(1.43, true), "143.0%");

    assert.equal(field(renderer, "Machine start").props.min, "2025-11-25");
    await act(async () => field(renderer, "Machine start").props.onChange({ target: { value: "2026-11-05" } }));
    assert.match(requests.at(-1), /kind=machine.*start_date=2026-11-05/);
    await act(async () => field(renderer, "Machine ID").props.onChange({ target: { value: "ins-actual" } }));
    assert.match(requests.at(-1), /machine_id=ins-actual/);
    await act(async () => field(renderer, "Machine class").props.onChange({ target: { value: "cvm" } }));
    assert.match(requests.at(-1), /resource_class=cvm/);
    const next = renderer.root.findAllByType("button").find((node) => node.children[0] === "Next");
    await act(async () => next.props.onClick());
    assert.match(requests.at(-1), /page=2/);
    await act(async () => field(renderer, "Region").props.onChange({ target: { value: "ap-hongkong" } }));
    assert.match(requests.at(-1), /region=ap-hongkong/);
    assert.match(requests.at(-1), /page=1/);

    const beforeKey = requests.length;
    await act(async () => field(renderer, "Label key").props.onChange({ target: { value: "test_project" } }));
    assert.equal(requests.length, beforeKey, "do not send a partial label filter");
    await act(async () => field(renderer, "Label value").props.onChange({ target: { value: "tidb/test" } }));
    assert.match(requests.at(-1), /kind=workload.*label_key=test_project&label_value=tidb%2Ftest/);
    await act(async () => field(renderer, "Group by label").props.onChange({ target: { value: "service" } }));
    assert.match(requests.at(-1), /group_by=service/);
    assert.doesNotMatch(requests.at(-1), /machine_id|resource_class|job_name/);
    const filterLabels = renderer.root.findAllByType("label").map((node) => node.findByType("span").children.join(""));
    assert.ok(!filterLabels.includes("Repo") && !filterLabels.includes("Stable Job"));
    assert.ok(requests.every((url) => url.startsWith("/api/v1/utilization/")));
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("machine interval summary is scoped by cluster and stays separate from paged day detail", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  globalThis.fetch = async (url) => {
    const params = new URL(url, "http://localhost").searchParams;
    if (!params.has("machine_id")) return response(url);
    const scoped = params.get("cluster_id") === "cls-example";
    const secondPage = params.get("page") === "2";
    return response(url, {
      ...machineData, start_date: "2026-11-16", end_date: "2026-11-24",
      ...(secondPage ? { has_more: false, items: [] } : {}),
      interval_summary: scoped ? {
        account_id: "100050658403", machine_id: "np-actual-machine", region: "ap-beijing", cluster_id: "cls-example",
        days_with_facts: 2, metrics: { cpu: { avg: 31.25, peak: 81.12, sample_minutes: 29 }, memory: missingMachineMetric },
      } : { reason: "ambiguous_machine" },
    });
  };
  const overview = () => renderer.root.findAllByProps({ "aria-label": "Machine interval summary" });
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.equal(overview().length, 0);
    await act(async () => field(renderer, "Machine ID").props.onChange({ target: { value: "np-actual-machine" } }));
    assert.match(JSON.stringify(renderer.toJSON()), /More than one physical machine matches/);
    assert.equal(overview().length, 0);
    assert.equal(renderer.root.findAllByProps({ "aria-label": "Machine utilization" }).length, 1);

    await act(async () => field(renderer, "Cluster ID").props.onChange({ target: { value: "cls-example" } }));
    const summaryText = () => overview()[0].findAllByType("p").map(text).join(" | ");
    const summary = summaryText();
    assert.match(summary, /np-actual-machine.*ap-beijing.*cls-example/);
    assert.match(summary, /2026-11-16.*2026-11-24.*Days with facts: 2.*weight each observed minute equally/);
    assert.match(summary, /CPU: Mean 31\.25% · Peak 81\.12% · Sample minutes: 29/);
    assert.match(summary, /Memory: Mean N\/A · Peak N\/A · Sample minutes: 0/);
    assert.equal(renderer.root.findAllByProps({ "aria-label": "Machine utilization" }).length, 1);

    const next = renderer.root.findAllByType("button").find((node) => node.children[0] === "Next");
    await act(async () => next.props.onClick());
    assert.equal(summaryText(), summary);
    assert.ok(renderer.root.findAllByType("span").some((node) => node.children.join("") === "Page 2"));
    assert.match(JSON.stringify(renderer.toJSON()), /No machine facts on this page/);
    assert.equal(renderer.root.findAllByProps({ "aria-label": "Workload utilization" }).length, 1);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

test("failed machine request leaves workloads visible, and changed filters hide stale machine rows", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  let resolveMachine;
  globalThis.fetch = async (url) => {
    if (String(url).includes("kind=machine")) return new Promise((resolve) => { resolveMachine = resolve; });
    return response(url);
  };
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.equal(renderer.root.findAllByProps({ "aria-label": "Workload utilization" }).length, 1);
    await act(async () => resolveMachine(response("/daily?kind=machine")));
    assert.match(JSON.stringify(renderer.toJSON()), /np-actual-machine/);
    await act(async () => field(renderer, "Machine class").props.onChange({ target: { value: "cvm" } }));
    assert.doesNotMatch(JSON.stringify(renderer.toJSON()), /np-actual-machine/);
    await act(async () => resolveMachine({ ok: false, status: 503, statusText: "Unavailable" }));
    assert.match(JSON.stringify(renderer.toJSON()), /503 Unavailable/);
    assert.equal(renderer.root.findAllByProps({ "aria-label": "Workload utilization" }).length, 1);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

for (const history of [false, true]) {
  test(`Pod collection off ${history ? "with" : "without"} retained statistics`, async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    let renderer;
    const { active_contexts, workload_window, label_keys, ...machineStatus } = readyStatus;
    const status = {
      ...machineStatus, latest_machine_usage_date: null, pod_collection_enabled: false,
      workload_history_available: history, ...(history ? { workload_window, label_keys } : {}),
    };
    const workload = {
      ...workloadData,
      items: [{ group_value: null, metrics: { cpu: { avg: 0, coverage: 1 }, memory: { avg: null, coverage: null } } }],
    };
    globalThis.fetch = async (url) => {
      requests.push(String(url));
      const group_by = new URL(url, "http://localhost").searchParams.get("group_by");
      return response(url, { ...machineData, items: [] }, { ...workload, group_by }, status);
    };
    try {
      await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
      const content = JSON.stringify(renderer.toJSON());
      assert.match(content, /Latest saved machine observation \(Beijing date\): .*None saved/);
      assert.match(content, /Pod collection: .*not enabled/);
      assert.match(content, /Task backlog: .*machines only/);
      assert.match(content, /No machine facts for these dates and filters/);
      assert.doesNotMatch(content, /Active Pod contexts/);
      assert.ok(requests.some((url) => url.includes("kind=machine")));
      assert.equal(requests.some((url) => url.includes("kind=workload")), history);
      if (history) {
        assert.match(content, /Workload results are retained history/);
        assert.equal(field(renderer, "Workload start").props.min, "2026-11-16");
        await act(async () => field(renderer, "Group by label").props.onChange({ target: { value: "service" } }));
        const cells = renderer.root.findByProps({ "aria-label": "Workload utilization" }).findAllByType("td");
        assert.match(text(cells[0]), /service: \(unlabelled\)/);
        assert.equal(text(cells[1]), "Mean 0.0% · Coverage 100.0%");
        assert.equal(text(cells[2]), "Mean N/A · Coverage N/A");
      } else {
        assert.doesNotMatch(content, /Workload business labels|Group by label|Mean 0\.0%/);
      }
    } finally {
      await act(async () => renderer?.unmount());
      globalThis.fetch = originalFetch;
    }
  });
}

for (const [reason, message] of Object.entries({
  query_too_large: /10,000 workload label-set rows/,
  missing_schema: /V3 utilization tables are not installed/,
  invalid_data: /Stored measurements are invalid/,
  pod_collection_disabled: /Pod collection is not enabled and no retained workload statistics are available/,
})) {
  test(`${reason} workload response is informative, not a partial or zero summary`, async () => {
    const originalFetch = globalThis.fetch;
    let renderer;
    const status = reason === "pod_collection_disabled"
      ? { ...readyStatus, pod_collection_enabled: false, workload_history_available: true } : readyStatus;
    globalThis.fetch = async (url) => response(url, machineData, { available: false, reason, items: [] }, status);
    try {
      await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
      assert.equal(renderer.root.findAllByProps({ "aria-label": "Workload utilization" }).length, 0);
      assert.equal(renderer.root.findAllByProps({ "aria-label": "Machine utilization" }).length, 1);
      const content = JSON.stringify(renderer.toJSON());
      assert.match(content, message);
      assert.doesNotMatch(content, /Partial data:/);
    } finally {
      await act(async () => renderer?.unmount());
      globalThis.fetch = originalFetch;
    }
  });
}
