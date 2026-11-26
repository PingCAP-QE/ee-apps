import { useState } from "react";

import { EmptyState, PageIntro, Panel } from "../components/charts";
import { useApiData } from "../lib/api";

const REASONS = {
  disabled: "Utilization is disabled. An operator must opt in with CI_DASHBOARD_UTILIZATION_ENABLED=true after collection readiness checks.",
  missing_schema: "V3 utilization tables are not installed. Existing dashboard pages are unaffected.",
  database_unavailable: "The utilization database is unavailable. No measurements are assumed to be zero.",
  unconfigured: "V3 utilization collection is not configured. No measurements are available yet.",
  pod_collection_disabled: "Pod collection is not enabled and no retained workload statistics are available.",
  query_too_large: "More than 10,000 workload label-set rows match. Narrow the dates or label filter; no partial aggregation is shown.",
  invalid_data: "Stored measurements are invalid. No partial aggregation is shown.",
};

export function utilizationPercent(value, fraction = false, digits = 1) {
  return value == null ? "N/A" : `${(Number(value) * (fraction ? 100 : 1)).toFixed(digits)}%`;
}

function numeric(value) {
  return value == null ? "N/A" : Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 });
}

export default function UtilizationPage() {
  const status = useApiData("/api/v1/utilization/status");
  const ready = status.data?.version === 3 && Boolean(status.data.available && status.data.configured);
  const podEnabled = status.data?.pod_collection_enabled;
  const podStopped = status.data?.pod_collection_stopped;
  const workloadDraining = status.data?.workload_draining;
  const workloadHistory = status.data?.workload_history_available;
  const workloadReady = ready && Boolean(podEnabled || workloadDraining || workloadHistory);
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
  const workload = useApiData("/api/v1/utilization/daily", workloadParams, workloadReady);
  const machineLoading = machine.loading || machine.responseKey !== JSON.stringify(machineParams);
  const workloadLoading = workload.loading || workload.responseKey !== JSON.stringify(workloadParams);
  const labels = status.data?.label_keys || [];

  return (
    <div className="page-stack">
      <PageIntro eyebrow="Resource Utilization" title="Tencent CICD · 100050658403"
        description="Beijing dates. Machine-day facts are retained for one year; workload label statistics are retained for the current and previous Beijing week. Missing measurements are not zero." />
      <Panel title="Collection status" loading={status.loading} error={status.error}>
        {!ready ? <EmptyState message={REASONS[status.data?.reason] || "Utilization is not ready."} /> : <>
          <p>Latest saved machine observation (Beijing date): {status.data.latest_machine_usage_date ?? "None saved"}. This is not a measure of data completeness.</p>
          <p>Collection: {status.data.collection_enabled ? "enabled" : "disabled"} · Maintenance: {status.data.maintenance_stale ? "stale / missing" : "current"} · Health: {status.data.healthy ? "healthy" : "unhealthy"}</p>
          {!status.data.healthy && <p>Health checks: {status.data.health_reasons?.join(", ") || "unavailable"}</p>}
          {status.data.paused && <p role="status">Collection paused: {(status.data.pause_reasons || []).join(", ") || "reason unavailable"}</p>}
          <p>Pod collection: {podEnabled ? "enabled" : podStopped ? "stopped" : "not enabled"}
            {workloadDraining ? " · Still settling confirmed workload windows" : !podEnabled && workloadHistory && " · Workload results are retained history"}
          </p>
          {podEnabled && <p>Active Pod contexts: {numeric(status.data.active_contexts)}</p>}
          <p>Task backlog: {podEnabled || workloadDraining ? "machines and Pods" : "machines only"} · pending {numeric(status.data.task_backlog?.pending)}, running {numeric(status.data.task_backlog?.running)}, retry {numeric(status.data.task_backlog?.retry)}</p>
        </>}
      </Panel>
      {ready && (
        <Panel title="Machine daily detail" subtitle="Select dates within the last 365 Beijing days. Enter an exact machine ID for an interval summary. A machine peak is its largest observed one-minute mean, not an instantaneous or group peak. Missing observations do not tell you whether a machine was idle or absent.">
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
                <thead><tr>{["Beijing date", "Machine identity", "Class", "CPU", "Memory"].map((label) => <th key={label}>{label}</th>)}</tr></thead>
                <tbody>{(machine.data?.items || []).map((item) => <tr key={`${item.usage_date}:${item.region}:${item.cluster_id}:${item.machine_id}`}>
                  <td>{item.usage_date}</td><td>{item.machine_id}<br />{item.region}<br />{item.cluster_id || "No cluster (CVM)"}</td>
                  <td>{item.resource_class}</td>
                  <td><MachineMetric value={item.metrics?.cpu} /></td><td><MachineMetric value={item.metrics?.memory} /></td>
                </tr>)}</tbody>
              </table></div>
            </Measurements>
            <Pagination page={machinePage} hasMore={machine.data?.has_more} onChange={setMachinePage} />
          </Panel>
        </Panel>
      )}
      {workloadReady && (
        <Panel title="Workload business labels" subtitle="Filter any approved business label and optionally group by one label across the selected dates. Mean utilization divides total usage by total requests for Pod minutes with observations and positive requests.">
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
          <p>Coverage is matched Pod minutes divided by expected Pod minutes. Missing labels appear as an unlabelled group. These statistics do not measure machine utilization or allocate costs.</p>
          <Panel title="Workload CPU / memory" loading={workloadLoading} error={workload.error}>
            {workload.data?.partial && <p role="status">Partial data: some workload windows in these dates are not settled. Coverage shown is for settled windows only.</p>}
            <Measurements data={workload.data} empty="No retained workload statistics for these dates and labels.">
              <div className="table-scroll"><table className="data-table data-table--compact" aria-label="Workload utilization">
                <thead><tr><th>Dates / business group</th><th>CPU</th><th>Memory</th></tr></thead>
                <tbody>{(workload.data?.items || []).map((item) => <tr key={JSON.stringify(item.group_value)}>
                  <td>{workload.data.start_date} – {workload.data.end_date}<br />{workload.data.group_by ? `${workload.data.group_by}: ${item.group_value ?? "(unlabelled)"}` : "All matched workloads"}</td>
                  <td><WorkloadMetric value={item.metrics?.cpu} /></td><td><WorkloadMetric value={item.metrics?.memory} /></td>
                </tr>)}</tbody>
              </table></div>
            </Measurements>
            <Pagination page={workloadPage} hasMore={workload.data?.has_more} onChange={setWorkloadPage} />
          </Panel>
        </Panel>
      )}
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
    <p>{data.start_date} – {data.end_date} Beijing · Days with facts: {summary.days_with_facts}. Means weight each observed minute equally, not each day.</p>
    {[["CPU", "cpu"], ["Memory", "memory"]].map(([label, key]) => <p key={key}>
      {label}: <MachineMetric value={summary.metrics?.[key]} digits={2} />
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

function MachineMetric({ value, digits = 1 }) {
  return <>Mean {utilizationPercent(value?.avg, false, digits)} · Peak {utilizationPercent(value?.peak, false, digits)} · Sample minutes: {numeric(value?.sample_minutes)}</>;
}

function WorkloadMetric({ value }) {
  return <>Mean {utilizationPercent(value?.avg)} · Coverage {utilizationPercent(value?.coverage, true)}</>;
}
