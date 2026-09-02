import {
  formatCurrency,
  formatDateRangeLabel,
  formatPercent,
  useApiData,
} from "../lib/api";
import { PageIntro, Panel, StatCard } from "../components/charts";

export default function WeeklyCostPage() {
  const report = useApiData("/api/v1/pages/weekly-cost");
  const summary = report.data?.summary || {};
  const lastWeek = report.data?.last_week || {};
  const previousWeek = report.data?.previous_week || {};
  const previousMonth = report.data?.previous_month || {};

  return (
    <div className="page-stack weekly-cost">
      <PageIntro
        eyebrow="Weekly Cost"
        title="QA cloud spend, ready for the weekly review"
        description="This page is fixed to the previous complete natural week, Monday through Sunday in UTC. QA accounts are sources with a configured purpose."
        kicker={formatDateRangeLabel(lastWeek.start_date, lastWeek.end_date)}
      />

      <section className="stats-grid weekly-cost__summary">
        <StatCard
          label="Last week cost"
          value={formatCurrency(summary.last_week_cost)}
          detail={formatDateRangeLabel(lastWeek.start_date, lastWeek.end_date)}
          delta={formatWeekOverWeek(summary.week_wow_pct)}
          deltaTone={costDeltaTone(summary.week_wow_pct)}
        />
        <StatCard
          label="Previous week cost"
          value={formatCurrency(summary.previous_week_cost)}
          detail={formatDateRangeLabel(previousWeek.start_date, previousWeek.end_date)}
          tone="teal"
        />
        <StatCard
          label="Last natural month cost"
          value={formatCurrency(summary.previous_month_cost)}
          detail={formatDateRangeLabel(previousMonth.start_date, previousMonth.end_date)}
          tone="amber"
        />
      </section>

      <Panel
        title="QA account breakdown"
        subtitle="Costs are net costs. QA share is each account's share of all QA accounts in the previous complete week."
        loading={report.loading}
        error={report.error}
      >
        {report.data?.items?.length ? (
          <div className="table-scroll">
            <table className="data-table weekly-cost__table">
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Purpose</th>
                  <th scope="col">Last week</th>
                  <th scope="col">WoW</th>
                  <th scope="col">QA share</th>
                  <th scope="col">Last natural month</th>
                </tr>
              </thead>
              <tbody>
                {report.data.items.map((item) => (
                  <tr key={item.cost_source}>
                    <th scope="row">
                      <span className="weekly-cost__account">
                        <strong>{`${String(item.vendor || "").toUpperCase()} · ${item.display_name || item.account_id}`}</strong>
                        <small>{item.account_id}</small>
                      </span>
                    </th>
                    <td className="weekly-cost__purpose">{item.purpose}</td>
                    <td>{formatCurrency(item.last_week_cost)}</td>
                    <td className={`weekly-cost__delta weekly-cost__delta--${costDeltaTone(item.week_wow_pct)}`}>
                      {formatWeekOverWeek(item.week_wow_pct)}
                    </td>
                    <td>{formatNullablePercent(item.last_week_share_pct)}</td>
                    <td>{formatCurrency(item.previous_month_cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : !report.loading && !report.error ? (
          <div className="empty-state">
            {report.data?.meta?.purpose_schema_available === false
              ? "QA source metadata is not deployed yet."
              : "No QA cost sources with a configured purpose."}
          </div>
        ) : null}
      </Panel>
    </div>
  );
}

function formatWeekOverWeek(value) {
  if (value === null || value === undefined) {
    return "WoW —";
  }
  const numeric = Number(value);
  return `WoW ${numeric > 0 ? "+" : ""}${formatPercent(numeric)}`;
}

function formatNullablePercent(value) {
  return value === null || value === undefined ? "—" : formatPercent(value);
}

function costDeltaTone(value) {
  if (value === null || value === undefined) {
    return "neutral";
  }
  const numeric = Number(value);
  if (numeric < 0) {
    return "improved";
  }
  if (numeric > 0) {
    return "regressed";
  }
  return "neutral";
}
