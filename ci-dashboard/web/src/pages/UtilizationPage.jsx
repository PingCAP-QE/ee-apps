import { useState } from "react";

import { EmptyState, PageIntro, Panel, TrendChart } from "../components/charts";
import { useApiData } from "../lib/api";

const REASONS = {
  disabled: "Utilization is disabled. An operator must opt in with CI_DASHBOARD_UTILIZATION_ENABLED=true after collection readiness checks.",
  missing_schema: "Utilization tables are not installed. Existing dashboard pages are unaffected.",
  database_unavailable: "The utilization database is unavailable. No measurements are assumed to be zero.",
  unconfigured: "Utilization collection is not configured. No measurements are available yet.",
};

export function utilizationPercent(value, fraction = false) {
  return value == null ? "N/A" : `${(Number(value) * (fraction ? 100 : 1)).toFixed(1)}%`;
}

export function weeklyTrendPoints(items) {
  if (!items.length) return [];
  const byWeek = new Map(items.map((item) => [item.week_start, item.status === "final_missing" ? null : item.values?.avg ?? null]));
  const weeks = [...byWeek.keys()].sort();
  const points = [];
  for (const day = new Date(`${weeks[0]}T00:00:00Z`); day.toISOString().slice(0, 10) <= weeks.at(-1); day.setUTCDate(day.getUTCDate() + 7)) {
    const label = day.toISOString().slice(0, 10);
    points.push([label, byWeek.get(label) ?? null]);
  }
  return points;
}

function numeric(value) {
  return value == null ? "N/A" : Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 });
}

function timestamp(value) {
  if (!value) return "Unknown";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short",
  }).format(new Date(value));
}

export default function UtilizationPage() {
  const status = useApiData("/api/v1/utilization/status");
  const ready = Boolean(status.data?.available && status.data?.configured);
  const [filters, setFilters] = useState({ scope: "job", repo: "", job_name: "", resource_class: "", resource_spec: "" });
  const [dates, setDates] = useState({});
  const [page, setPage] = useState(1);
  const [weeklyFilters, setWeeklyFilters] = useState({ resource_class: "tke_native", metric: "cpu" });
  const [weeklyDates, setWeeklyDates] = useState({});
  const [weeklyPage, setWeeklyPage] = useState(1);
  const dailyDates = { ...status.data?.daily_window, ...dates };
  const daily = useApiData("/api/v1/utilization/daily", { ...dailyDates, ...filters, page, page_size: 50 }, ready);
  const weekly = useApiData("/api/v1/utilization/weekly", { ...weeklyDates, ...weeklyFilters, page: weeklyPage, page_size: 100 }, ready);
  const dailyItems = daily.data?.items || [];
  const weeklyItems = weekly.data?.items || [];

  function changeFilter(key, value) {
    setPage(1);
    setFilters((current) => key === "scope"
      ? { ...current, scope: value, repo: "", job_name: "", resource_spec: "" }
      : { ...current, [key]: value });
  }

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="Resource Utilization"
        title="Tencent CICD · 100050658403"
        description="Beijing dates and Monday weeks. Daily summaries refresh at 00:30 / 06:30 / 12:30 / 18:30, not in real time. Only the current and previous week retain repo / Job details; older history is account + resource class only."
      />
      <Panel title="Collection status" loading={status.loading} error={status.error}>
        {!ready ? <EmptyState message={REASONS[status.data?.reason] || "Utilization is not ready."} /> : (
          <>
            <p>Data cutoff: {timestamp(status.data.latest_cutoff)} Beijing · {status.data.stale ? "Stale / missing" : "Within refresh window"}</p>
            <p>Collection: {status.data.collection_enabled ? "enabled" : "disabled"} · Maintenance: {status.data.maintenance_stale ? "stale / missing" : "healthy"}</p>
            {status.data.paused && <p role="status">Collection paused: {(status.data.pause_reasons || []).join(", ") || "reason unavailable"}</p>}
            <p>Missing account-summary dates: {status.data.missing_daily_dates?.join(", ") || "None reported"}. Recent final incomplete / missing scopes (up to 100 in the past year): {status.data.collection_gaps?.length ?? "Unknown"}.</p>
            <p>Unknown repo and Job usage is retained, not redistributed. Metric coverage and quality are shown below; metadata field counts are not usage-weighted attribution coverage.</p>
          </>
        )}
      </Panel>
      {ready && <>
        <Panel title="Daily optimization · repo → stable Job → resource spec"
          subtitle="Job / repo utilization uses requests; physical resource / account-class utilization uses physical capacity. These scopes are never added together. Full-coverage and observed-subset peaks are separate synchronous minute-mean maxima, never sums of individual peaks.">
          <div className="filter-grid">
            <Field label="Daily start"><input type="date" min={status.data.daily_window.start_date} max={status.data.daily_window.end_date} value={dailyDates.start_date || ""} onChange={(event) => { setDates({ ...dailyDates, start_date: event.target.value }); setPage(1); }} /></Field>
            <Field label="Daily end"><input type="date" min={status.data.daily_window.start_date} max={status.data.daily_window.end_date} value={dailyDates.end_date || ""} onChange={(event) => { setDates({ ...dailyDates, end_date: event.target.value }); setPage(1); }} /></Field>
            <Field label="Stored scope"><select value={filters.scope} onChange={(event) => changeFilter("scope", event.target.value)}>
              <option value="job">Stable Job · requests basis</option><option value="repo">Repo · requests basis</option>
              <option value="resource">Physical resource · capacity basis</option><option value="account_class">Account class · capacity basis</option>
            </select></Field>
            <Field label="Resource class"><input value={filters.resource_class} maxLength={512} onChange={(event) => changeFilter("resource_class", event.target.value)} placeholder="All classes" /></Field>
            {["job", "repo"].includes(filters.scope) && <Field label="Repo"><input value={filters.repo} maxLength={512} onChange={(event) => changeFilter("repo", event.target.value)} placeholder="org/repo or unknown_repo" /></Field>}
            {filters.scope === "job" && <Field label="Stable Job"><input value={filters.job_name} maxLength={512} onChange={(event) => changeFilter("job_name", event.target.value)} placeholder="Stable path or unknown_job" /></Field>}
            {["job", "resource"].includes(filters.scope) && <Field label="Resource spec"><input value={filters.resource_spec} maxLength={512} onChange={(event) => changeFilter("resource_spec", event.target.value)} placeholder="Exact stored spec" /></Field>}
          </div>
          <p>Precise Job cost: unavailable (no verified same-dimension cost evidence). No projected personal chargeback or savings estimate is used.</p>
          <p>Attribution evidence stops at the displayed cutoff. Late metadata does not automatically improve successfully published dates after next-day 18:30.</p>
        </Panel>
        <Panel title="Daily CPU / memory" loading={daily.loading} error={daily.error}>
          {daily.data?.available === false ? <EmptyState message={REASONS[daily.data.reason] || "Measurements unavailable."} /> : !dailyItems.length ? <EmptyState message="No stored summaries for this scope. Missing measurements are not 0%." /> : (
            <div className="table-scroll"><table className="data-table data-table--compact" aria-label="Daily utilization">
              <thead><tr>{["Date / state", "Repo → stable Job", "Resource class / spec", "CPU", "Memory", "Cutoff / evidence (Beijing)"].map((label) => <th key={label}>{label}</th>)}</tr></thead>
              <tbody>{dailyItems.map((item) => <tr key={`${item.usage_date}:${item.resource_key}`}>
                <td>{item.usage_date}<br />{item.status}</td>
                <td>{item.repo || "Not applicable"}<br />{item.job_name || "Not applicable"}</td>
                <td>{item.resource_class}<br />{item.resource_spec || "N/A"}<br />{item.namespace} {item.region}</td>
                <td><Metric value={item.metrics?.cpu} /></td><td><Metric value={item.metrics?.memory} /></td>
                <td>{timestamp(item.cutoff)}<br />Evidence: {timestamp(item.evidence_cutoff)}</td>
              </tr>)}</tbody>
            </table></div>
          )}
          <Pagination page={page} hasMore={daily.data?.has_more} onChange={setPage} />
        </Panel>
        <Panel title="Account weekly trends" subtitle="Physical account-class summaries only; no historical Job detail or Pod curves. Select one class and metric. Nulls and final_missing are gaps, never zero.">
          <div className="filter-grid">
            <Field label="Weekly resource class"><select value={weeklyFilters.resource_class} onChange={(event) => { setWeeklyFilters({ ...weeklyFilters, resource_class: event.target.value }); setWeeklyPage(1); }}>
              <option value="tke_native">TKE native nodes</option><option value="tke_cvm">TKE CVM-backed nodes</option><option value="cvm">CVM</option>
            </select></Field>
            <Field label="Weekly metric"><select value={weeklyFilters.metric} onChange={(event) => { setWeeklyFilters({ ...weeklyFilters, metric: event.target.value }); setWeeklyPage(1); }}>
              <option value="cpu">CPU</option><option value="memory">Memory</option>
            </select></Field>
            <Field label="Weekly start"><input type="date" value={weeklyDates.start_date || ""} onChange={(event) => { setWeeklyDates({ ...weeklyDates, start_date: event.target.value }); setWeeklyPage(1); }} /></Field>
            <Field label="Weekly end"><input type="date" value={weeklyDates.end_date || ""} onChange={(event) => { setWeeklyDates({ ...weeklyDates, end_date: event.target.value }); setWeeklyPage(1); }} /></Field>
          </div>
          <p>Default: last 52 Beijing weeks. Supply both dates for a custom range of less than one calendar year; dates select week starts. Pagination is capped at 100 pages; narrow filters for larger result sets.</p>
        </Panel>
        <Panel title="Weekly CPU / memory · selected class" loading={weekly.loading} error={weekly.error}>
          {weekly.data?.available === false ? <EmptyState message={REASONS[weekly.data.reason] || "Measurements unavailable."} /> : !weeklyItems.length ? <EmptyState message="No account weekly measurements for this class." /> : <>
            <TrendChart series={[{ key: "average", label: `${weeklyFilters.resource_class} ${weeklyFilters.metric} mean`, color: "#1f4e79", type: "line", points: weeklyTrendPoints(weeklyItems) }]} yFormatter={utilizationPercent} ariaLabel="Account weekly utilization trend" />
            <div className="table-scroll"><table className="data-table data-table--compact" aria-label="Weekly utilization"><thead><tr><th>Beijing week</th><th>Class / metric / basis</th><th>Mean / peak / coverage</th><th>State / finalization</th></tr></thead><tbody>
              {weeklyItems.map((item) => <tr key={`${item.week_start}:${item.resource_class}:${item.metric}`}>
                <td>{item.week_start}</td><td>{item.resource_class} / {item.metric}<br />{item.contract?.denominator || "Unknown basis"}</td>
                <td><Metric value={{ ...item.values, contract: item.contract }} /></td><td>{item.status}<br />{timestamp(item.finalized_at)}</td>
              </tr>)}
            </tbody></table></div>
          </>}
          <Pagination page={weeklyPage} hasMore={weekly.data?.has_more} onChange={setWeeklyPage} />
        </Panel>
      </>}
    </div>
  );
}

function Field({ label, children }) {
  return <label className="filter-field"><span>{label}</span>{children}</label>;
}

function Pagination({ page, hasMore, onChange }) {
  return <div className="panel__actions"><button disabled={page <= 1} onClick={() => onChange(page - 1)}>Previous</button><span>Page {page}</span><button disabled={!hasMore || page >= 100} onClick={() => onChange(page + 1)}>Next</button></div>;
}

function Metric({ value }) {
  if (!value) return <>N/A · no measurement</>;
  const fullPeak = value.peak_coverage === 1 ? value.peak : null;
  const subset = value.peak_subset ?? (value.peak_coverage !== 1 ? value.peak : null);
  const subsetCoverage = value.peak_subset != null ? value.peak_subset_coverage : value.peak_coverage;
  return <>
    <div>Mean {utilizationPercent(value.avg)} · Full peak {utilizationPercent(fullPeak)}</div>
    {subset != null && <div>Observed subset peak {utilizationPercent(subset)} · coverage {utilizationPercent(subsetCoverage, true)} (not a full-population peak)</div>}
    <div>Coverage {utilizationPercent(value.coverage, true)} · Peak coverage {utilizationPercent(value.peak_coverage, true)}</div>
    <details><summary>Actual / denominator / quality</summary>
      <div>Basis: {value.contract?.denominator || "Unknown"} · {value.contract?.normalized_unit || "unknown unit"}·s</div>
      <div>Total actual integral: {numeric(value.total_used_integral)}</div>
      <div>Matched actual / capacity integrals: {numeric(value.used_integral)} / {numeric(value.capacity_integral)}</div>
      <div>Matched / expected seconds: {numeric(value.observed_seconds)} / {numeric(value.expected_seconds)}</div>
      <div>Usage-observed seconds: {numeric(value.usage_observed_seconds ?? value.observed_seconds)}</div>
      <div>Full peak at: {timestamp(fullPeak != null ? value.peak_at : null)} Beijing</div>
      {subset != null && <div>Subset peak at: {timestamp(value.peak_subset_at ?? value.peak_at)} Beijing</div>}
      <div>Quality: {value.quality?.join(", ") || "Not reported"}</div>
    </details>
  </>;
}
