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

before(async () => {
  server = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  ({ default: UtilizationPage, utilizationPercent } = await server.ssrLoadModule("/src/pages/UtilizationPage.jsx"));
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
});

after(async () => { await server?.close(); });

const readyStatus = {
  available: true, configured: true, collection_enabled: true, stale: false, healthy: false,
  latest_cutoff: "2026-11-24T04:00:00Z", workload_cutoff: "2026-11-24T03:00:00Z",
  maintenance_stale: false, paused: true, pause_reasons: ["budget"], active_contexts: 12,
  task_backlog: { pending: 2, running: 1, retry: 3 },
  label_keys: ["repo", "job", "test_project", "test_suite", "environment", "project", "service"],
  machine_window: { start_date: "2025-11-25", end_date: "2026-11-24" },
  default_window: { start_date: "2026-11-16", end_date: "2026-11-24" },
  workload_window: { start_date: "2026-11-16", end_date: "2026-11-24" },
};
const metric = {
  avg: null, coverage: 0.53, total_used: 70, used: 50, capacity: 200,
  expected_capacity: 300, observed_seconds: 40, expected_seconds: 60,
  usage_seconds: 60, unknown_seconds: 10, zero_seconds: 10,
};
const machineData = {
  available: true, has_more: true, kind: "machine", items: [{
    usage_date: "2026-11-24", machine_id: "np-actual-machine", region: "ap-beijing",
    cluster_id: "cls-example", resource_class: "tke_native", instance_type: "4c8g",
    cpu_cores: 4, memory_bytes: 8589934592, cutoff: "2026-11-24T04:00:00Z",
    metrics: { cpu: { ...metric, peak: 143.12, peak_at: "2026-11-24T03:00:00Z" } },
  }],
};
const workloadData = {
  available: true, has_more: false, kind: "workload", group_by: null,
  start_date: "2026-11-16", end_date: "2026-11-24",
  items: [{ group_value: null, cutoff: "2026-11-24T03:00:00Z", metrics: { cpu: metric, memory: metric } }],
};

function field(renderer, label) {
  return renderer.root.findAllByType("label").find((node) => node.findByType("span").children.join("") === label)
    .findAll((node) => node.type === "input" || node.type === "select")[0];
}

function response(url, machine = machineData, workload = workloadData) {
  const parsed = new URL(url, "http://localhost");
  const data = parsed.pathname.endsWith("/status") ? readyStatus
    : parsed.searchParams.get("kind") === "machine" ? machine : workload;
  return { ok: true, json: async () => data };
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

test("machine identity, dates, peaks and workload absolute usage are separate; no weekly requests", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  let renderer;
  globalThis.fetch = async (url) => { requests.push(String(url)); return response(url); };
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    assert.equal(requests.length, 3);
    assert.ok(requests.some((url) => url.includes("kind=machine") && url.includes("start_date=2026-11-16")));
    assert.ok(requests.some((url) => url.includes("kind=workload")));
    assert.ok(!requests.some((url) => url.includes("/weekly")));
    const content = JSON.stringify(renderer.toJSON());
    for (const text of ["np-actual-machine", "cls-example", "ap-beijing", "2026-11-24", "N/A", "143.1%", "53.0%", "Absolute usage:", "70", "core·s", "byte·s", "Collection paused:", "Task backlog:", "Last machine cutoff:", "Last workload cutoff:"]) {
      assert.ok(content.includes(text), text);
    }
    assert.doesNotMatch(content, /Mean 0\.0%|Account weekly|Observed subset|Full peak|repo →/);
    assert.match(content, /Precise Job cost: unavailable/);
    const workload = renderer.root.findByProps({ "aria-label": "Workload utilization" });
    assert.ok(!workload.findAllByType("div").some((node) => node.children.join("").includes("peak")));
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

test("unknown and zero requests preserve usage; unobserved usage is N/A and measured zero is zero", async () => {
  const originalFetch = globalThis.fetch;
  let renderer;
  const data = structuredClone(workloadData);
  data.group_by = "service";
  data.items = [
    { group_value: "unknown-requests", metrics: { cpu: { ...metric, avg: null, unknown_seconds: 60, zero_seconds: 0 }, memory: { ...metric, avg: null, unknown_seconds: 0, zero_seconds: 60 } } },
    { group_value: null, metrics: { cpu: { ...metric, avg: null, usage_seconds: 0, total_used: 0 }, memory: { ...metric, avg: 0, usage_seconds: 60, total_used: 0 } } },
  ];
  globalThis.fetch = async (url) => response(url, machineData, data);
  try {
    await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
    const rows = renderer.root.findByProps({ "aria-label": "Workload utilization" }).findByType("tbody").findAllByType("tr");
    const cells = rows[1].findAllByType("td");
    const cellText = (cell) => cell.findAllByType("div").map((node) => node.children.join("")).join(" | ");
    assert.match(cellText(rows[0].findAllByType("td")[1]), /Mean N\/A.*Absolute usage: 70 core·s/);
    assert.match(cellText(rows[0].findAllByType("td")[2]), /Mean N\/A.*Absolute usage: 70 byte·s/);
    assert.match(cellText(cells[1]), /N\/A \(no usage observations\)/);
    assert.match(cellText(cells[2]), /Absolute usage: 0 byte·s/);
    assert.match(cellText(cells[2]), /Mean 0\.0%/);
    assert.match(JSON.stringify(renderer.toJSON()), /\(missing label\)/);
  } finally {
    await act(async () => renderer?.unmount());
    globalThis.fetch = originalFetch;
  }
});

for (const reason of ["query_too_large", "missing_schema", "invalid_data"]) {
  test(`${reason} workload response is informative, not a partial or zero summary`, async () => {
    const originalFetch = globalThis.fetch;
    let renderer;
    globalThis.fetch = async (url) => response(url, machineData, { available: false, reason, items: [] });
    try {
      await act(async () => { renderer = TestRenderer.create(React.createElement(UtilizationPage)); });
      assert.equal(renderer.root.findAllByProps({ "aria-label": "Workload utilization" }).length, 0);
      assert.equal(renderer.root.findAllByProps({ "aria-label": "Machine utilization" }).length, 1);
      const content = JSON.stringify(renderer.toJSON());
      if (reason === "query_too_large") assert.match(content, /10,000 workload label-set rows/);
      if (reason === "missing_schema") assert.match(content, /V2 utilization tables are not installed/);
      if (reason === "invalid_data") assert.match(content, /Stored measurements are invalid/);
    } finally {
      await act(async () => renderer?.unmount());
      globalThis.fetch = originalFetch;
    }
  });
}
