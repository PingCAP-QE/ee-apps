import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import React, { useMemo, useState } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { createPath, parsePath, Router } from "react-router-dom";
import { createServer } from "vite";

const cachedCostUrl = "/cost?start_date=2026-07-27&end_date=2026-08-31&cost_source=gcp%3Apingcap-testing-account&granularity=week";
const ciStatusUrl = "/ci-status?start_date=2026-08-10&end_date=2026-08-10&granularity=week";
const incomingCostUrl = "/cost?start_date=2026-08-10&end_date=2026-08-10&cost_source=gcp%3Apingcap-testing-account&granularity=week";

let App;
let server;

before(async () => {
  server = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  ({ default: App } = await server.ssrLoadModule("/src/App.jsx"));
});

after(async () => {
  await server?.close();
});

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
