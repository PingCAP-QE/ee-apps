import { useState } from "react";

import { EmptyState, PageIntro, Panel } from "../components/charts";
import { useApiData } from "../lib/api";

const REASONS = {
  disabled: "Utilization is disabled. An operator must opt in with CI_DASHBOARD_UTILIZATION_ENABLED=true after collection readiness checks.",
  missing_schema: "V2 utilization tables are not installed. Existing dashboard pages are unaffected.",
  database_unavailable: "The utilization database is unavailable. No measurements are assumed to be zero.",
  unconfigured: "V2 utilization collection is not configured. No measurements are available yet.",
  query_too_large: "More than 10,000 workload label-set rows match. Narrow the dates or label filter; no partial aggregation is shown.",
  invalid_data: "Stored measurements are invalid. No partial aggregation is shown.",
};

export function utilizationPercent(value, fraction = false, digits = 1) {
  return value == null ? "N/A" : `${(Number(value) * (fraction ? 100 : 1)).toFixed(digits)}%`;
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
  const [machineFilters, setMachineFilters] = useState({ machine_id: "", region: "", cluster_id: "", resource_class: "" });
  const [machineDates, setMachineDates] = useState({});
  const [machinePage, setMachinePage] = useState(1);
  const [workloadFilters, setWorkloadFilters] = useState({ label_key: "", label_value: "", group_by: "" });
  const [workloadDates, setWorkloadDates] = useState({});
  const [workloadPage, setWorkloadPage] = useState(1);
  const machineWindow = { ...status.data?.default_window, ...machineDates };
  const workloadWindow = { ...status.data?.workload_window, ...workloadDates };
  const machineParams = { kind: "machine", ...machineWindow, ...machineFilters, page: machinePage, page_size: 50 };
  const workloadParams = {
    kind: "workload", ...workloadWindow, group_by: workloadFilters.group_by,
    ...(workloadFilters.label_key && workloadFilters.label_value
      ? { label_key: workloadFilters.label_key, label_value: workloadFilters.label_value } : {}),
    page: workloadPage, page_size: 50,
  };
  const machine = useApiData("/api/v1/utilization/daily", machineParams, ready);
  const workload = useApiData("/api/v1/utilization/daily", workloadParams, ready);
  const machineLoading = machine.loading || machine.responseKey !== JSON.stringify(machineParams);
  const workloadLoading = workload.loading || workload.responseKey !== JSON.stringify(workloadParams);
  const labels = status.data?.label_keys || [];

  return (
    <div className="page-stack">
      <PageIntro eyebrow="Resource Utilization" title="Tencent CICD · 100050658403"
        description="Beijing dates. Machine-day facts retain one year; workload label statistics retain the current and previous Beijing week. Physical machine usage and workload consumption are separate, never added together. Missing measurements are not zero." />
      <Panel title="Collection status" loading={status.loading} error={status.error}>
        {!ready ? <EmptyState message={REASONS[status.data?.reason] || "Utilization is not ready."} /> : <>
          <p>Last machine cutoff: {timestamp(status.data.latest_cutoff)} Beijing · {status.data.stale ? "Stale / missing" : "Within refresh window"}</p>
          <p>Last workload cutoff: {timestamp(status.data.workload_cutoff)} Beijing</p>
          <p>Collection: {status.data.collection_enabled ? "enabled" : "disabled"} · Maintenance: {status.data.maintenance_stale ? "stale / missing" : "current"} · Health: {status.data.healthy ? "healthy" : "unhealthy"}</p>
          {!status.data.healthy && <p>Health checks: {status.data.health_reasons?.join(", ") || "unavailable"}</p>}
          {status.data.paused && <p role="status">Collection paused: {(status.data.pause_reasons || []).join(", ") || "reason unavailable"}</p>}
          <p>Active Pod contexts: {numeric(status.data.active_contexts)} · Task backlog: pending {numeric(status.data.task_backlog?.pending)}, running {numeric(status.data.task_backlog?.running)}, retry {numeric(status.data.task_backlog?.retry)}</p>
        </>}
      </Panel>
      {ready && <>
        <Panel title="Machine daily detail" subtitle="Physical-capacity basis. Select dates within the last 365 Beijing days. Enter an exact machine ID for an interval summary. Peaks apply only to that machine, never a resource group.">
          <div className="filter-grid">
            <Dates prefix="Machine" dates={machineWindow} bounds={status.data.machine_window} onChange={(key, value) => { setMachineDates({ ...machineWindow, [key]: value }); setMachinePage(1); }} />
            <Field label="Machine ID"><input value={machineFilters.machine_id} maxLength={512} placeholder="All machines" onChange={(event) => { setMachineFilters({ ...machineFilters, machine_id: event.target.value }); setMachinePage(1); }} /></Field>
            <Field label="Region"><input value={machineFilters.region} maxLength={512} placeholder="All regions" onChange={(event) => { setMachineFilters({ ...machineFilters, region: event.target.value }); setMachinePage(1); }} /></Field>
            <Field label="Cluster ID"><input value={machineFilters.cluster_id} maxLength={512} placeholder="All clusters" onChange={(event) => { setMachineFilters({ ...machineFilters, cluster_id: event.target.value }); setMachinePage(1); }} /></Field>
            <Field label="Machine class"><select value={machineFilters.resource_class} onChange={(event) => { setMachineFilters({ ...machineFilters, resource_class: event.target.value }); setMachinePage(1); }}>
              <option value="">All classes</option><option value="cvm">CVM</option><option value="tke_native">TKE native node</option>
            </select></Field>
          </div>
          <Panel title="Machine CPU / memory" loading={machineLoading} error={machine.error}>
            <MachineIntervalSummary data={machine.data} />
            <Measurements data={machine.data} empty={machinePage > 1 ? "No machine facts on this page." : "No machine facts for these dates and filters."}>
              <div className="table-scroll"><table className="data-table data-table--compact" aria-label="Machine utilization">
                <thead><tr>{["Beijing date", "Machine identity", "Class / historical spec", "CPU", "Memory", "Cutoff (Beijing)"].map((label) => <th key={label}>{label}</th>)}</tr></thead>
                <tbody>{(machine.data?.items || []).map((item) => <tr key={`${item.usage_date}:${item.region}:${item.cluster_id}:${item.machine_id}`}>
                  <td>{item.usage_date}</td><td>{item.machine_id}<br />{item.region}<br />{item.cluster_id || "No cluster (CVM)"}</td>
                  <td>{item.resource_class}<br />{item.instance_type || "N/A"}<br />{numeric(item.cpu_cores)} cores / {numeric(item.memory_bytes)} bytes</td>
                  <td><Metric value={item.metrics?.cpu} unit="core·s" machine /></td><td><Metric value={item.metrics?.memory} unit="byte·s" machine /></td>
                  <td>{timestamp(item.cutoff)}</td>
                </tr>)}</tbody>
              </table></div>
            </Measurements>
            <Pagination page={machinePage} hasMore={machine.data?.has_more} onChange={setMachinePage} />
          </Panel>
        </Panel>
        <Panel title="Workload business labels" subtitle="Requests basis. Filter any approved business label and optionally group by one label across the selected dates. Averages use summed matched integrals, not averages of percentages. No workload or group peaks.">
          <div className="filter-grid">
            <Dates prefix="Workload" dates={workloadWindow} bounds={status.data.workload_window} onChange={(key, value) => { setWorkloadDates({ ...workloadWindow, [key]: value }); setWorkloadPage(1); }} />
            <Field label="Label key"><select value={workloadFilters.label_key} onChange={(event) => { setWorkloadFilters({ ...workloadFilters, label_key: event.target.value, label_value: "" }); setWorkloadPage(1); }}>
              <option value="">No label filter</option>{labels.map((key) => <option key={key} value={key}>{key}</option>)}
            </select></Field>
            <Field label="Label value"><input value={workloadFilters.label_value} disabled={!workloadFilters.label_key} maxLength={512} placeholder="Exact label value" onChange={(event) => { setWorkloadFilters({ ...workloadFilters, label_value: event.target.value }); setWorkloadPage(1); }} /></Field>
            <Field label="Group by label"><select value={workloadFilters.group_by} onChange={(event) => { setWorkloadFilters({ ...workloadFilters, group_by: event.target.value }); setWorkloadPage(1); }}>
              <option value="">All matched workloads</option>{labels.map((key) => <option key={key} value={key}>{key}</option>)}
            </select></Field>
          </div>
          <p>Missing labels do not discard usage. Unknown or zero requests make efficiency unavailable, but known absolute usage remains visible. No individual Pod history or personal attribution. Precise Job cost: unavailable.</p>
          <Panel title="Workload CPU / memory" loading={workloadLoading} error={workload.error}>
            {workload.data?.partial && <p role="status">Partial data: some workload windows in these dates are not settled. Coverage shown is for settled windows only.</p>}
            <Measurements data={workload.data} empty="No retained workload statistics for these dates and labels.">
              <div className="table-scroll"><table className="data-table data-table--compact" aria-label="Workload utilization">
                <thead><tr><th>Dates / business group</th><th>CPU</th><th>Memory</th><th>Oldest contributing cutoff (Beijing)</th></tr></thead>
                <tbody>{(workload.data?.items || []).map((item) => <tr key={item.group_value ?? "missing"}>
                  <td>{workload.data.start_date} – {workload.data.end_date}<br />{workload.data.group_by ? `${workload.data.group_by}: ${item.group_value ?? "(missing label)"}` : "All matched workloads"}</td>
                  <td><Metric value={item.metrics?.cpu} unit="core·s" /></td><td><Metric value={item.metrics?.memory} unit="byte·s" /></td><td>{timestamp(item.cutoff)}</td>
                </tr>)}</tbody>
              </table></div>
            </Measurements>
            <Pagination page={workloadPage} hasMore={workload.data?.has_more} onChange={setWorkloadPage} />
          </Panel>
        </Panel>
      </>}
    </div>
  );
}

function Field({ label, children }) {
  return <label className="filter-field"><span>{label}</span>{children}</label>;
}

function Dates({ prefix, dates, bounds, onChange }) {
  return ["start", "end"].map((key) => <Field key={key} label={`${prefix} ${key}`}><input type="date" min={bounds?.start_date} max={bounds?.end_date} value={dates[`${key}_date`] || ""} onChange={(event) => { if (event.target.value) onChange(`${key}_date`, event.target.value); }} /></Field>);
}

function MachineIntervalSummary({ data }) {
  const summary = data?.interval_summary;
  if (!summary) return null;
  if (summary.reason === "ambiguous_machine") {
    return <p>More than one physical machine matches. Narrow by region, cluster ID, or class to see an interval summary.</p>;
  }
  if (summary.reason) return <p>Interval summary unavailable: stored measurements are invalid.</p>;
  return <section aria-label="Machine interval summary">
    <h4>Machine interval summary</h4>
    <p>{summary.machine_id} · {summary.region} · {summary.cluster_id || "No cluster (CVM)"}</p>
    <p>{data.start_date} – {data.end_date} Beijing · Days with facts: {summary.days_with_facts}. Missing dates are not filled in.</p>
    {[["CPU", "cpu"], ["Memory", "memory"]].map(([label, key]) => <p key={key}>
      {label} weighted mean {utilizationPercent(summary.metrics[key].avg, false, 2)} · Peak {utilizationPercent(summary.metrics[key].peak, false, 2)}
    </p>)}
  </section>;
}

function Measurements({ data, empty, children }) {
  if (data?.available === false) return <EmptyState message={REASONS[data.reason] || "Measurements unavailable."} />;
  if (!data?.items?.length) return <EmptyState message={`${empty} Missing measurements are not 0%.`} />;
  return children;
}

function Pagination({ page, hasMore, onChange }) {
  return <div className="panel__actions"><button disabled={page <= 1} onClick={() => onChange(page - 1)}>Previous</button><span>Page {page}</span><button disabled={!hasMore || page >= 1000} onClick={() => onChange(page + 1)}>Next</button></div>;
}

function Metric({ value, unit, machine = false }) {
  if (!value) return <>N/A · no measurement</>;
  return <>
    <div>Mean {utilizationPercent(value.avg)} · Coverage {utilizationPercent(value.coverage, true)}</div>
    {machine && <div>Machine peak {utilizationPercent(value.peak)} at {timestamp(value.peak_at)} Beijing</div>}
    <div>Absolute usage: {value.usage_seconds > 0 ? numeric(value.total_used) : "N/A (no usage observations)"} {unit}</div>
    <details><summary>Integrals / coverage / requests quality</summary>
      <div>Matched usage / capacity: {numeric(value.used)} / {numeric(value.capacity)} {unit}</div>
      <div>Expected capacity: {numeric(value.expected_capacity)} {unit}</div>
      <div>Matched / expected seconds: {numeric(value.observed_seconds)} / {numeric(value.expected_seconds)}</div>
      <div>Usage-observed seconds: {numeric(value.usage_seconds)}</div>
      <div>Unknown denominator seconds: {numeric(value.unknown_seconds)} · Zero denominator seconds: {numeric(value.zero_seconds)}</div>
    </details>
  </>;
}
