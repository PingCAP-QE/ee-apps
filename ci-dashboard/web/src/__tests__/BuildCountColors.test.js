import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createServer } from "vite";

let BuildTrendPage;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(), appType: "custom", logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: BuildTrendPage } = await server.ssrLoadModule("/src/pages/BuildTrendPage.jsx"));
});

after(async () => server?.close());

const namesByDimension = {
  Repo: ["pingcap/tidb", "tikv/pd", "pingcap/tiflash", "pingcap/tiflow", "pingcap/ticdc", "PingCAP-QE/ci", "pingcap-inc/tidb", "pingcap/tidb-operator", "Others"],
  Branch: ["master", "release-8.5", "main", "release-nextgen-202609", "feature/release-8.5.5-active-active", "feature/release-8.5-materialized-view", "release-8.5-20261109-v8.5.8", "release-8.5-20261103-v8.5.4", "Others"],
  Author: ["(unknown author)", "ti-chi-bot", "rleungx", "windtalker", "terry1purcell", "lhy1024", "D3Hunter", "0xPoe", "Others"],
};
const weeks = ["2026-11-08", "2026-11-09"];
const breakdown = Object.fromEntries(Object.entries(namesByDimension).map(([dimension, names]) => [
  dimension.toLowerCase(),
  {
    items: names.map((name, index) => ({ name, value: 2 * (100 - index) })),
    series: names.map((label, index) => ({
      key: `${dimension.toLowerCase()}:${label}`, label, type: "bar",
      points: weeks.map((week) => [week, 100 - index]),
    })),
  },
]));

function legendColors(chart) {
  return Object.fromEntries(chart.findAllByProps({ className: "chart-legend__item" }).map((item) => [
    item.findAllByType("span")[1].children.join(""),
    item.findByProps({ className: "chart-legend__swatch" }).props.style.backgroundColor,
  ]));
}

for (const [dimension, names] of Object.entries(namesByDimension)) {
  test(`build-count ${dimension} colors are distinct, consistent and stable on selection`, async () => {
    const originalFetch = globalThis.fetch;
    const originalWindow = globalThis.window;
    let renderer;
    globalThis.fetch = async () => ({
      ok: true, json: async () => ({ build_count_breakdown_trend: breakdown }),
    });
    globalThis.window = { setTimeout(callback) { callback(); return 1; }, clearTimeout() {} };

    try {
      await act(async () => {
        renderer = TestRenderer.create(React.createElement(BuildTrendPage, { filters: {} }));
      });
      await act(async () => renderer.root
        .findByProps({ "aria-label": "Build count grouping" })
        .findAllByType("button").find((button) => button.children.join("") === dimension).props.onClick());
      const chart = () => renderer.root.findByProps({ className: "build-count-breakdown" });
      const initialColors = legendColors(chart());
      assert.deepEqual(Object.keys(initialColors), names);
      assert.equal(new Set(Object.values(initialColors)).size, 9);

      const assertBarColors = (visibleNames) => {
        const bars = chart().findAllByType("rect").filter((bar) => bar.props.opacity === "0.78");
        assert.deepEqual(bars.map((bar) => bar.props.fill),
          visibleNames.flatMap((name) => weeks.map(() => initialColors[name])));
      };
      const assertTooltipColors = async (visibleNames) => {
        await act(async () => chart().findAllByProps({ className: "chart-hit-area" })[1].props.onMouseEnter());
        const rows = chart().findByProps({ className: "chart-tooltip" })
          .findAllByProps({ className: "chart-tooltip__text" }).map((text) => text.parent);
        assert.deepEqual(rows.map((row) => row.findByType("circle").props.fill),
          visibleNames.map((name) => initialColors[name]));
        assert.deepEqual(rows.map((row) => row.findByType("text").children.join("").split(": ")[0]), visibleNames);
        await act(async () => chart().findAllByProps({ className: "chart-hit-area" })[1].props.onMouseLeave());
      };
      assertBarColors(names);
      await assertTooltipColors(names);

      for (const name of names) {
        await act(async () => chart().findByProps({ "aria-label": "Build count trend dimension selector" })
          .findAllByType("button").find((button) => button.findAllByType("span")[0]?.children.join("") === name).props.onClick());
        assert.deepEqual(legendColors(chart()), { [name]: initialColors[name] });
        assertBarColors([name]);
        await assertTooltipColors([name]);
        await act(async () => chart().findByProps({ "aria-label": "Build count trend dimension selector" })
          .findAllByType("button")[0].props.onClick());
        assert.deepEqual(legendColors(chart()), initialColors);
      }
    } finally {
      await act(async () => renderer?.unmount());
      globalThis.fetch = originalFetch;
      globalThis.window = originalWindow;
    }
  });
}
