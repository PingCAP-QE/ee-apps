import { useEffect, useState } from "react";

import { fetchJson } from "../lib/api";

// Keep the aggregate's four-query window, but publish each independent HTTP result
// immediately. A gateway timeout in one section cannot swallow the other responses.
const SECTIONS = [
  "period_comparison", "distinct_flaky_case_counts", "issue_case_rates", "chart_sections",
  "issue_sections", "top_jobs", "failure_category_share", "failure_category_trend",
];
const PENDING = { loading: true };

export function useFlakySections(filters) {
  const key = JSON.stringify(filters);
  const [page, setPage] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    const queue = SECTIONS.values();
    setPage({ key });
    async function load() {
      for (const section of queue) {
        if (controller.signal.aborted) return;
        let result;
        try {
          const data = await fetchJson(`/api/v1/pages/flaky/${section}`, filters, controller.signal);
          result = { loading: false, data };
        } catch (error) {
          const status = error?.message?.match(/^(\d{3}) /)?.[1];
          result = { loading: false, error: status ? `Section unavailable (HTTP ${status})` : "Section unavailable" };
        }
        if (controller.signal.aborted) return;
        setPage((previous) => ({ ...previous, [section]: result }));
        // A proxy error need not mean the underlying SQL has stopped. Retire this
        // slot instead of dispatching another query on top of possibly orphaned work.
        if (result.error) return;
      }
    }
    Promise.all(Array.from({ length: 4 }, load)).then(() => {
      if (controller.signal.aborted) return;
      for (const section of queue) {
        setPage((previous) => ({ ...previous, [section]: {
          loading: false, error: "Section not requested after earlier errors",
        } }));
      }
    });
    return () => controller.abort();
  }, [key]);
  return (section) => (page?.key === key && page[section]) || PENDING;
}
