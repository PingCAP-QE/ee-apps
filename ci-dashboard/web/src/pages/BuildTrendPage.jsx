import { useEffect, useState } from "react";

import {
  formatCompact,
  formatDelta,
  formatPercent,
  formatSeconds,
  sumSeriesPoints,
  useApiData,
} from "../lib/api";
import {
  DonutShareChart,
  DrilldownModal,
  PageIntro,
  Panel,
  RankingList,
  StatCard,
  TrendChart,
} from "../components/charts";

export default function BuildTrendPage({ filters }) {
  const page = useApiData("/api/v1/pages/ci-status", filters);
  const [selectedRepoSlice, setSelectedRepoSlice] = useState(null);

  const outcomeSummary = page.data?.outcome_trend?.meta?.summary || {};
  const totalBuilds = Number(
    outcomeSummary.total_count ?? sumSeriesPoints(page.data?.outcome_trend?.series, "total_count"),
  );
  const successBuilds = Number(
    outcomeSummary.success_count ?? sumSeriesPoints(page.data?.outcome_trend?.series, "success_count"),
  );
  const failureBuilds = Number(
    outcomeSummary.failure_count ?? sumSeriesPoints(page.data?.outcome_trend?.series, "failure_count"),
  );
  const successRate = Number(
    outcomeSummary.success_rate_pct ?? (totalBuilds ? (successBuilds * 100) / totalBuilds : 0),
  );
  const durationSummary = page.data?.duration_trend?.meta?.summary || {};
  const avgQueue = Number(durationSummary.queue_avg_s || 0);
  const avgRun = Number(durationSummary.run_avg_s || 0);
  const avgTotal = Number(durationSummary.total_avg_s || 0);
  const successRateDelta = computeSeriesDelta(
    page.data?.outcome_trend?.series,
    "success_rate_pct",
  );
  const outcomeRightAxisMin = computeCenteredPercentAxisMin(
    page.data?.outcome_trend?.series,
    "success_rate_pct",
  );
  const queueDeltaSeconds = computeSeriesDelta(
    page.data?.duration_trend?.series,
    "queue_avg_s",
  );
  const totalDeltaSeconds = computeSeriesDelta(
    page.data?.duration_trend?.series,
    "total_avg_s",
  );
  const cloudRepoShare = page.data?.cloud_repo_share?.clouds || [];
  const gcpRepoShare = limitRepoShareItems(
    cloudRepoShare.find((cloud) => cloud.cloud_phase === "GCP"),
  );
  const idcRepoShare = limitRepoShareItems(
    cloudRepoShare.find((cloud) => cloud.cloud_phase === "IDC"),
  );

  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setSelectedRepoSlice(null);
      }
    }

    if (!selectedRepoSlice) {
      return undefined;
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedRepoSlice]);

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="CI Status"
        title="Capacity and reliability trends before we start diagnosing root causes"
        description="This page is for answering whether CI is slowing down, failing more often, or drifting between cloud environments."
      />

      <section className="stats-grid">
        <StatCard
          label="Total builds"
          value={formatCompact(totalBuilds)}
          detail={`${formatCompact(successBuilds)} success · ${formatCompact(failureBuilds)} failure-like`}
        />
        <StatCard
          label="Success rate"
          value={formatPercent(successRate)}
          detail="Counted from build outcomes in the current view"
          tone="teal"
          delta={successRateDelta == null ? null : formatDelta(successRateDelta + 0, 0, "%")}
        />
        <StatCard
          label="Success avg queue wait"
          value={formatSeconds(avgQueue)}
          detail={`Success avg run time ${formatSeconds(avgRun)}`}
          tone="amber"
          delta={queueDeltaSeconds == null ? null : formatDelta(queueDeltaSeconds + 0, 0, "s")}
        />
        <StatCard
          label="Success avg total duration"
          value={formatSeconds(avgTotal)}
          detail="Simple average across successful builds"
          tone="default"
          delta={totalDeltaSeconds == null ? null : formatDelta(totalDeltaSeconds + 0, 0, "s")}
        />
      </section>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Outcome trend"
          subtitle="Count trend for total, success, and failure-like builds. Success rate uses the right axis."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.outcome_trend?.series}
            rightYFormatter={formatPercent}
            rightYMin={outcomeRightAxisMin}
            rightYMax={100}
          />
        </Panel>

        <Panel
          title="Duration trend"
          subtitle="Success-only queue, run, and end-to-end duration trends over the same buckets."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart series={page.data?.duration_trend?.series} yFormatter={formatSeconds} />
        </Panel>
        <Panel
          title="Longest avg success time jobs"
          subtitle="Success-only average run time ranked inside the current repo, branch, cloud, and date scope."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.longest_avg_success_jobs?.items}
            valueFormatter={formatSeconds}
            renderMeta={(item) => [
              <span key={`${item.name}-counts`}>
                {formatCompact(item.success_build_count)} success / {formatCompact(item.total_build_count)} total
              </span>,
              <span key={`${item.name}-rate`}>Success rate {formatPercent(item.success_rate_pct)}</span>,
            ]}
          />
        </Panel>

        <Panel
          title="Lowest success rate jobs"
          subtitle="Jobs sorted by success rate for the current scope, with build counts shown so small-sample jobs are easy to spot."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.lowest_success_rate_jobs?.items}
            valueFormatter={formatPercent}
            renderMeta={(item) => [
              <span key={`${item.name}-ratio`}>
                {formatCompact(item.success_build_count)} / {formatCompact(item.total_build_count)} succeeded
              </span>,
              <span key={`${item.name}-avg`}>
                {item.success_build_count
                  ? `Avg success ${formatSeconds(item.success_avg_run_s)}`
                  : "No successful runs"}
              </span>,
            ]}
          />
        </Panel>
      </div>

      <Panel
        title="Build Count Rate grouped by Repo"
        subtitle="Compare repo build-count share on GCP and IDC. Each chart merges repos below 1% into Others, then keeps the top 10 slices, ignores repo and cloud filters, and lets you drill into repo branch mix."
        loading={page.loading}
        error={page.error}
      >
        <div className="donut-grid">
          <DonutShareChart
            title="GCP repo share"
            subtitle="Build count split by repo on GCP."
            items={gcpRepoShare?.items}
            onItemSelect={(item) =>
              setSelectedRepoSlice({
                cloudPhase: "GCP",
                totalBuilds: gcpRepoShare?.total_builds || 0,
                ...item,
              })
            }
            emptyMessage="No GCP repo-share data for the current filters."
          />
          <DonutShareChart
            title="IDC repo share"
            subtitle="Build count split by repo on IDC."
            items={idcRepoShare?.items}
            onItemSelect={(item) =>
              setSelectedRepoSlice({
                cloudPhase: "IDC",
                totalBuilds: idcRepoShare?.total_builds || 0,
                ...item,
              })
            }
            emptyMessage="No IDC repo-share data for the current filters."
          />
        </div>
      </Panel>

      <Panel
        title="Selected job trend"
        subtitle={buildSelectedJobTrendSubtitle(filters.job_name)}
        loading={page.loading}
        error={page.error}
      >
        {!filters.job_name ? (
          <div className="empty-state">Please select a job.</div>
        ) : (
          <TrendChart
            series={buildSelectedJobTrendSeries(
              page.data?.outcome_trend?.series,
              page.data?.duration_trend?.series,
              filters,
            )}
            yFormatter={formatSeconds}
            rightYFormatter={formatPercent}
            rightYMax={100}
          />
        )}
      </Panel>

      {selectedRepoSlice ? (
        <DrilldownModal
          title={`${selectedRepoSlice.name} branch mix`}
          subtitle={`${selectedRepoSlice.cloudPhase} · ${formatCompact(selectedRepoSlice.value)} builds · ${formatPercent(selectedRepoSlice.share_pct)} of ${selectedRepoSlice.cloudPhase}`}
          onClose={() => setSelectedRepoSlice(null)}
        >
          <DonutShareChart
            title={`${selectedRepoSlice.cloudPhase} branch share`}
            subtitle="Branch split inside the selected repo."
            items={selectedRepoSlice.branches}
            totalLabel="builds"
            emptyMessage="No branch share data for this repo."
          />
        </DrilldownModal>
      ) : null}
    </div>
  );
}

function limitRepoShareItems(cloudShare, maxItems = 10, minSharePct = 1) {
  if (!cloudShare) {
    return cloudShare;
  }

  const items = cloudShare.items || [];
  if (!items.length) {
    return cloudShare;
  }

  const totalValue = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const largeItems = [];
  const smallItems = [];

  items.forEach((item) => {
    if (Number(item.share_pct || 0) < minSharePct) {
      smallItems.push(item);
      return;
    }
    largeItems.push(item);
  });

  const topItems = largeItems.slice(0, maxItems);
  const overflowItems = largeItems.slice(maxItems);
  const otherItems = [...smallItems, ...overflowItems];

  if (!otherItems.length && topItems.length === items.length) {
    return cloudShare;
  }

  const otherValue = otherItems.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const mergedItems = [...topItems];

  if (otherValue > 0) {
    mergedItems.push({
      name: "Others",
      value: otherValue,
      share_pct: totalValue ? (otherValue * 100) / totalValue : 0,
      branches: [],
      interactive: false,
    });
  }

  return {
    ...cloudShare,
    items: mergedItems,
  };
}

function computeSeriesDelta(series, key) {
  const target = series?.find((item) => item.key === key);
  if (!target?.points || target.points.length < 2) {
    return null;
  }
  const current = Number(target.points[target.points.length - 1][1] || 0);
  const previous = Number(target.points[target.points.length - 2][1] || 0);
  return current - previous;
}

function computeCenteredPercentAxisMin(series, key, fixedMax = 100) {
  const target = series?.find((item) => item.key === key);
  const values =
    target?.points
      ?.map((point) => Number(point[1]))
      .filter((value) => Number.isFinite(value)) || [];

  if (!values.length) {
    return 0;
  }

  const average = values.reduce((sum, value) => sum + value, 0) / values.length;
  const minValue = Math.min(...values);
  const centeredMin = average * 2 - fixedMax;
  const visibilityPadding = Math.max(Math.min((fixedMax - minValue) * 0.08, 3), 1);
  const maxVisibleMin = Math.max(0, minValue - visibilityPadding);

  return Number(Math.max(0, Math.min(centeredMin, maxVisibleMin)).toFixed(2));
}

function buildSelectedJobTrendSubtitle(jobName) {
  if (!jobName) {
    return "Track a selected job's success rate and success avg total duration over the current bucket setting.";
  }

  return (
    <>
      Track <strong>{jobName}</strong> success rate and success avg total duration over the
      current bucket setting.
    </>
  );
}

function buildSelectedJobTrendSeries(outcomeSeries, durationSeries, filters) {
  const successRateSeries = outcomeSeries?.find((item) => item.key === "success_rate_pct");
  const durationSeriesItem = durationSeries?.find((item) => item.key === "total_avg_s");
  const bucketLabels = buildBucketLabels(
    filters?.start_date,
    filters?.end_date,
    filters?.granularity,
  );
  const effectiveLabels = bucketLabels.length
    ? bucketLabels
    : Array.from(
        new Set([
          ...(successRateSeries?.points.map((point) => point[0]) || []),
          ...(durationSeriesItem?.points.map((point) => point[0]) || []),
        ]),
      ).sort();
  const successRateByBucket = new Map(successRateSeries?.points || []);
  const durationByBucket = new Map(durationSeriesItem?.points || []);
  const series = [];

  if (durationSeriesItem) {
    series.push({
      key: "selected_job_total_avg_s",
      label: "Success avg total duration",
      type: "bar",
      axis: "left",
      points: effectiveLabels.map((label) => [label, durationByBucket.get(label) ?? 0]),
    });
  }

  if (successRateSeries) {
    series.push({
      key: "selected_job_success_rate_pct",
      label: "Success rate",
      type: "line",
      axis: "right",
      points: effectiveLabels.map((label) => [
        label,
        successRateByBucket.has(label) ? successRateByBucket.get(label) : null,
      ]),
    });
  }

  return series;
}

function buildBucketLabels(startDate, endDate, granularity) {
  if (!startDate || !endDate) {
    return [];
  }

  const start = parseIsoDate(startDate);
  const end = parseIsoDate(endDate);
  if (!start || !end || start > end) {
    return [];
  }

  if (granularity === "week") {
    const bounds = completeWeekBounds(start, end);
    if (!bounds) {
      return [];
    }

    const labels = [];
    const cursor = new Date(bounds.firstStart);
    while (cursor <= bounds.lastStart) {
      labels.push(toIsoDate(cursor));
      cursor.setUTCDate(cursor.getUTCDate() + 7);
    }
    return labels;
  }

  const labels = [];
  const cursor = new Date(start);
  while (cursor <= end) {
    labels.push(toIsoDate(cursor));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return labels;
}

function completeWeekBounds(start, end) {
  const firstStart = new Date(start);
  firstStart.setUTCDate(firstStart.getUTCDate() + ((7 - ((firstStart.getUTCDay() + 6) % 7)) % 7));

  const lastStart = new Date(end);
  lastStart.setUTCDate(lastStart.getUTCDate() - ((lastStart.getUTCDay() + 6) % 7));
  if (end.getUTCDay() !== 0) {
    lastStart.setUTCDate(lastStart.getUTCDate() - 7);
  }

  if (firstStart > lastStart) {
    return null;
  }

  return { firstStart, lastStart };
}

function parseIsoDate(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  if (!year || !month || !day) {
    return null;
  }
  return new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
}

function toIsoDate(value) {
  return value.toISOString().slice(0, 10);
}
