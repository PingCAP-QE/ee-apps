import { useEffect, useState } from "react";

import { fetchJson } from "../lib/api";

// Keep the aggregate's four-query window, but publish each independent HTTP result
// immediately. A gateway timeout in one section cannot swallow the other responses.
const SECTIONS = [
  "period_comparison", "distinct_flaky_case_counts", "issue_case_rates", "chart_sections",
  "issue_sections", "top_jobs", "failure_category_share", "failure_category_trend",
];
const PENDING = { loading: true };
const FLAKY_PATH = "/api/v1/pages/flaky";

function unavailable(status) {
  return { loading: false, error: status ? `Section unavailable (HTTP ${status})` : "Section unavailable" };
}

function httpStatus(error) {
  return error?.message?.match(/^(\d{3}) /)?.[1];
}

export function useFlakySections(filters) {
  const key = JSON.stringify(filters);
  const [page, setPage] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const queue = SECTIONS.values();
    const missing = [];
    setPage({ key });
    async function load() {
      while (!signal.aborted && !missing.length) {
        const { value: section, done } = queue.next();
        if (done) return;
        let result;
        try {
          const data = await fetchJson(`${FLAKY_PATH}/${section}`, filters, signal);
          result = { loading: false, data };
        } catch (error) {
          const status = httpStatus(error);
          if (status === "404" || status === "405") {
            missing.push(section);
            return;
          }
          result = unavailable(status);
        }
        if (signal.aborted) return;
        setPage((previous) => ({ ...previous, [section]: result }));
        // A proxy error need not mean the underlying SQL has stopped. Retire this
        // slot instead of dispatching another query on top of possibly orphaned work.
        if (result.error) return;
      }
    }
    Promise.all(Array.from({ length: 4 }, load)).then(async () => {
      if (signal.aborted) return;
      const pending = [...missing, ...queue];
      if (!pending.length) return;
      let result = { loading: false, error: "Section not requested after earlier errors" };
      if (missing.length) {
        // Old replicas lack section routes. Once the lanes settle, load the
        // aggregate once; its flat payload already contains each panel's keys.
        try {
          result = { loading: false, data: await fetchJson(FLAKY_PATH, filters, signal) };
        } catch (error) {
          result = unavailable(httpStatus(error));
        }
      }
      if (signal.aborted) return;
      setPage((previous) => ({
        ...previous, ...Object.fromEntries(pending.map((section) => [section, result])),
      }));
    });
    return () => controller.abort();
  }, [key]);
  return (section) => (page?.key === key && page[section]) || PENDING;
}
