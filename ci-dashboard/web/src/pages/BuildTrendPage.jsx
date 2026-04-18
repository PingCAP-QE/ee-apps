import { useEffect, useState } from "react";

import {
  formatCompact,
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
  RuntimeComparisonBoard,
  StatCard,
  TrendChart,
} from "../components/charts";

export default function BuildTrendPage({ filters }) {
  const page = useApiData("/api/v1/pages/ci-status", filters);
  const [selectedRepoSlice, setSelectedRepoSlice] = useState(null);

  const totalBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "total_count");
  const successBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "success_count");
  const failureBuilds = sumSeriesPoints(page.data?.outcome_trend?.series, "failure_count");
  const successRate = totalBuilds ? (successBuilds * 100) / totalBuilds : 0;
  const durationSummary = page.data?.duration_trend?.meta?.summary || {};
  const avgQueue = Number(durationSummary.queue_avg_s || 0);
  const avgRun = Number(durationSummary.run_avg_s || 0);
  const avgTotal = Number(durationSummary.total_avg_s || 0);
  const cloudPostureAnnotations = buildCloudPostureAnnotations(page.data?.cloud_posture_trend?.series);
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
        />
        <StatCard
          label="Success avg queue wait"
          value={formatSeconds(avgQueue)}
          detail={`Success avg run time ${formatSeconds(avgRun)}`}
          tone="amber"
        />
        <StatCard
          label="Success avg total duration"
          value={formatSeconds(avgTotal)}
          detail="Simple average across successful builds"
          tone="default"
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
      </div>

      <Panel
        title="Migration status"
        subtitle="Weekly build counts on IDC versus GCP, with each week labeled by GCP as a share of total builds."
        loading={page.loading}
        error={page.error}
      >
        <TrendChart
          series={page.data?.cloud_posture_trend?.series}
          bucketAnnotations={cloudPostureAnnotations}
          height={188}
        />
      </Panel>

      <div className="page-grid page-grid--two-column">
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
        title="Migration runtime comparison"
        subtitle="Same-job success run-time comparison. IDC baseline is the 14 days before first GCP success; GCP uses the latest 14 days ending at the selected end date. This panel ignores start date, bucket, and cloud filters."
        loading={page.loading}
        error={page.error}
      >
        <RuntimeComparisonBoard
          improved={page.data?.migration_runtime_comparison?.improved}
          regressed={page.data?.migration_runtime_comparison?.regressed}
          windowDays={page.data?.migration_runtime_comparison?.meta?.window_days}
          minSuccessRuns={page.data?.migration_runtime_comparison?.meta?.min_success_runs_each_side}
        />
      </Panel>

      <Panel
        title="Build Count Rate grouped by Repo"
        subtitle="Compare repo build-count share on GCP and IDC. Each chart keeps the top 10 repos, merges the rest into Others, ignores repo and cloud filters, and lets you drill into repo branch mix."
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
        title="Reading note"
        subtitle="How to use this page during the first V1 iterations."
      >
        <div className="narrative-card">
          <p>
            Start here when a team says "builds feel worse this week." Look for one of three
            shapes: a volume spike, a success-rate dip, or a duration climb. Once one of those
            shapes appears, the Flaky page helps answer whether the problem is mostly noisy test
            behavior or something less classified.
          </p>
          <ul>
            <li>Use repo and branch filters to avoid cross-team noise.</li>
            <li>Use cloud phase when you suspect migration or infra drift.</li>
            <li>Expect PR enrichment fields to be best effort, not perfect.</li>
          </ul>
        </div>
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

function buildCloudPostureAnnotations(series) {
  const gcpSeries = series?.find((item) => item.key === "gcp_build_count");
  const idcSeries = series?.find((item) => item.key === "idc_build_count");
  if (!gcpSeries || !idcSeries) {
    return [];
  }

  const gcpByLabel = new Map(gcpSeries.points.map(([label, value]) => [label, Number(value || 0)]));
  const idcByLabel = new Map(idcSeries.points.map(([label, value]) => [label, Number(value || 0)]));
  return Array.from(new Set([...gcpByLabel.keys(), ...idcByLabel.keys()]))
    .sort()
    .map((label) => {
      const gcpBuilds = gcpByLabel.get(label) || 0;
      const idcBuilds = idcByLabel.get(label) || 0;
      const totalBuilds = gcpBuilds + idcBuilds;
      return {
        label,
        text: formatPercent(totalBuilds ? (gcpBuilds * 100) / totalBuilds : 0),
      };
    });
}

function limitRepoShareItems(cloudShare, maxItems = 10) {
  if (!cloudShare) {
    return cloudShare;
  }

  const items = cloudShare.items || [];
  if (items.length <= maxItems) {
    return cloudShare;
  }

  const totalValue = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
  const topItems = items.slice(0, maxItems);
  const otherItems = items.slice(maxItems);
  const otherValue = otherItems.reduce((sum, item) => sum + Number(item.value || 0), 0);

  return {
    ...cloudShare,
    items: [
      ...topItems,
      {
        name: "Others",
        value: otherValue,
        share_pct: totalValue ? (otherValue * 100) / totalValue : 0,
        branches: [],
        interactive: false,
      },
    ],
  };
}
