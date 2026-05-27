import { useEffect, useState } from "react";

import {
  formatBrowserDateTime,
  formatCompact,
  formatNumber,
  formatPercent,
  formatSeconds,
  sumSeriesPoints,
  useApiData,
} from "../lib/api";
import {
  DonutShareChart,
  PageIntro,
  Panel,
  RankingList,
  ShareBars,
  StatCard,
  TrendChart,
} from "../components/charts";

const EMPTY_ITEMS = [];
const EMPTY_DETAILS = {};
const formatOneDecimal = (value) => Number(value || 0).toFixed(1);

function buildRateAxisBounds(values) {
  const numericValues = values.filter((value) => Number.isFinite(value));
  if (!numericValues.length) {
    return { min: 0, max: 10 };
  }

  const hardMin = 0;
  const hardMax = 95;
  const rawMin = Math.min(...numericValues);
  const rawMax = Math.max(...numericValues);
  const span = rawMax - rawMin;
  const pad = span === 0 ? Math.max(4, rawMax * 0.35 || 4) : Math.max(span, 4);

  let min = rawMin - pad;
  let max = rawMax + pad;
  const desiredRange = max - min;

  if (min < hardMin) {
    max = Math.min(hardMax, max + (hardMin - min));
    min = hardMin;
  }
  if (max > hardMax) {
    min = Math.max(hardMin, min - (max - hardMax));
    max = hardMax;
  }
  if (max - min < 1) {
    max = Math.min(hardMax, min + 1);
  }
  if (max - min < desiredRange && min === hardMin) {
    max = Math.min(hardMax, hardMin + desiredRange);
  }

  return {
    min: Number(min.toFixed(1)),
    max: Number(max.toFixed(1)),
  };
}

function buildSelectedTrendWithShare(selectedKey, allSeries, shareLabelSuffix) {
  if (!selectedKey || !allSeries?.length) {
    return { series: EMPTY_ITEMS, rightAxis: null };
  }

  const selectedSeries = allSeries.find((item) => item.key === selectedKey);
  if (!selectedSeries) {
    return { series: EMPTY_ITEMS, rightAxis: null };
  }

  const labels = Array.from(
    new Set(allSeries.flatMap((item) => item.points.map((point) => point[0]))),
  ).sort();
  const selectedValues = new Map(
    selectedSeries.points.map((point) => [point[0], Number(point[1] || 0)]),
  );

  const shareSeries = {
    key: "selected_share_pct",
    label: `${selectedSeries.label || selectedKey} ${shareLabelSuffix}`,
    type: "line",
    axis: "right",
    points: labels.map((label) => {
      const total = allSeries.reduce((sum, item) => {
        const point = item.points.find((candidate) => candidate[0] === label);
        return sum + Number(point?.[1] || 0);
      }, 0);
      const selectedValue = selectedValues.get(label) || 0;
      const sharePct = total > 0 ? (selectedValue / total) * 100 : 0;
      return [label, Number(sharePct.toFixed(1))];
    }),
  };

  return {
    series: [selectedSeries, shareSeries],
    rightAxis: buildRateAxisBounds(shareSeries.points.map((point) => point[1])),
  };
}

export default function RuntimeInsightsPage({ filters }) {
  const page = useApiData("/api/v1/pages/runtime-insights", filters);
  const [selectedL1, setSelectedL1] = useState("INFRA");
  const [selectedL2, setSelectedL2] = useState("");
  const [selectedJob, setSelectedJob] = useState("");

  const summary = page.data?.runtime_summary || {};
  const schedulingFailureMeta = page.data?.scheduling_failure_jobs?.meta || {};
  const schedulingSlowestMeta = page.data?.scheduling_slowest_jobs?.meta || {};
  const minFinalFailures = Number(schedulingFailureMeta.min_final_failures ?? 3);
  const filteredOutFailureJobs = Number(schedulingFailureMeta.filtered_out_job_count ?? 0);
  const minSchedulingWaitSeconds = Number(schedulingSlowestMeta.min_wait_seconds ?? 150);
  const schedulingWaitSupported = summary.scheduling_wait_supported ?? true;
  const schedulingWaitValue =
    schedulingWaitSupported && summary.avg_scheduling_wait_s != null
      ? formatSeconds(summary.avg_scheduling_wait_s)
      : "N/A";
  const schedulingWaitDetail = schedulingWaitSupported
    ? `Avg pod creation to Scheduled · ${formatCompact(summary.valid_scheduling_sample_count)} scheduled build samples`
    : "Full scheduling wait needs a pod creation timestamp; this DB does not have one yet.";
  const classificationCoverage = page.data?.classification_coverage || {};
  const classificationSummary = classificationCoverage.summary || {};
  const l1Items = page.data?.error_l1_share?.items || EMPTY_ITEMS;
  const l2Details = page.data?.error_l1_share?.l2_details || EMPTY_DETAILS;
  const selectedL1Trend = buildSelectedTrendWithShare(
    selectedL1,
    page.data?.error_l1_trend?.series || EMPTY_ITEMS,
    "share",
  );
  const selectedL2Share =
    selectedL1 === "INFRA"
      ? page.data?.infra_l2_share?.items || l2Details.INFRA || EMPTY_ITEMS
      : l2Details[selectedL1] || EMPTY_ITEMS;
  const selectedL2Trend = buildSelectedTrendWithShare(
    selectedL2,
    page.data?.error_l2_trends?.items?.[selectedL1]?.series || EMPTY_ITEMS,
    "share",
  );
  const topErrorJobs = useApiData("/api/v1/pages/runtime-error-top-jobs", {
    ...filters,
    error_l1_category: selectedL1 || "",
    error_l2_subcategory: selectedL2 || "",
    limit: 10,
  });
  const selectedErrorBuilds = useApiData(
    "/api/v1/pages/runtime-error-builds",
    {
      ...filters,
      selected_job_name: selectedJob || "",
      error_l1_category: selectedL1 || "",
      error_l2_subcategory: selectedL2 || "",
      limit: 15,
    },
    Boolean(selectedJob),
  );
  const classifiedCount = sumSeriesPoints(
    classificationCoverage.classified_vs_unclassified_trend?.series,
    "classified_count",
  );
  const humanRevisedCount =
    classificationSummary.human_revised_count ??
    classificationCoverage.machine_vs_revised?.groups?.[0]?.values?.[1] ??
    0;
  const specificClassifiedCount =
    classificationSummary.specific_classified_count ??
    Math.max(classifiedCount - (classificationSummary.machine_others_count || 0), 0);
  const pendingAnalyzeCount = classificationSummary.pending_analyze_count ?? 0;
  const machineOthersCount = classificationSummary.machine_others_count ?? 0;

  useEffect(() => {
    if (!l1Items.length) {
      return;
    }
    if (l1Items.some((item) => item.name === selectedL1)) {
      return;
    }
    setSelectedL1(l1Items[0].name);
  }, [l1Items, selectedL1]);

  useEffect(() => {
    if (!selectedL2Share.length) {
      if (selectedL2) {
        setSelectedL2("");
      }
      return;
    }
    if (selectedL2Share.some((item) => item.name === selectedL2)) {
      return;
    }
    setSelectedL2(selectedL2Share[0].name);
  }, [selectedL2Share, selectedL2]);

  useEffect(() => {
    const items = topErrorJobs.data?.items || EMPTY_ITEMS;
    if (!items.length) {
      if (selectedJob) {
        setSelectedJob("");
      }
      return;
    }
    if (items.some((item) => item.name === selectedJob)) {
      return;
    }
    setSelectedJob(items[0].name);
  }, [topErrorJobs.data, selectedJob]);

  const topJobsDrilldownLabel = selectedL2 ? `${selectedL1} -> ${selectedL2}` : selectedL1;

  return (
    <div className="page-stack">
      <PageIntro
        eyebrow="CI details insight"
        title="Find the runtime bottlenecks hiding between Jenkins and Kubernetes"
        description="An experimental tab for pod scheduling, image pull, and Jenkins error catalog signals. Useful panels can graduate into CI Status once the signal proves stable."
        kicker={classificationCoverage.latest_pod_event_at ? `Latest pod event ${classificationCoverage.latest_pod_event_at}` : null}
      />

      <section className="stats-grid">
        <StatCard
          label="Scheduling failures"
          value={formatCompact(summary.final_scheduling_failure_count)}
          detail="Failed to schedule within 30m"
          tone="rose"
        />
        <StatCard
          label="Scheduling wait"
          value={schedulingWaitValue}
          detail={schedulingWaitDetail}
          tone="amber"
        />
        <StatCard
          label="Image pull avg"
          value={formatSeconds(summary.avg_pull_image_s)}
          detail={`${formatCompact(summary.valid_pull_image_sample_count)} samples · ${formatPercent(summary.pull_image_success_rate_pct)} success rate`}
          tone="teal"
        />
        <StatCard
          label="Specific labels"
          value={formatCompact(specificClassifiedCount)}
          detail={`${formatCompact(pendingAnalyzeCount)} pending analyze · ${formatCompact(machineOthersCount)} machine OTHERS · ${formatCompact(humanRevisedCount)} revised`}
          tone="rose"
        />
      </section>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Scheduling wait and final failures"
          subtitle={
            schedulingWaitSupported
              ? "Bars show time from pod creation to Scheduled. The red line and right axis show final scheduling failures, meaning builds that failed to schedule within 30m."
              : "Full scheduling wait needs a pod creation timestamp, so the bar series is unavailable in the current DB. The red line and right axis still show final scheduling failures, meaning builds that failed to schedule within 30m."
          }
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.scheduling_trend?.series}
            yFormatter={formatSeconds}
            rightYFormatter={formatCompact}
            yTickMode="integer"
            rightYTickMode="integer"
            rightYMin={0}
          />
        </Panel>

        <Panel
          title="Image pull trend"
          subtitle="Build-level slowest pull duration, plus image-pull success rate for builds with pull evidence."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.pull_image_trend?.series}
            yFormatter={formatSeconds}
            rightYFormatter={formatPercent}
            rightYMin={0}
            rightYMax={100}
          />
        </Panel>

        <Panel
          title="Final scheduling failure jobs"
          subtitle={`Jobs ranked by builds whose linked pods failed to schedule within 30m; showing only jobs with >${minFinalFailures} failures. Filtered out ${formatCompact(filteredOutFailureJobs)} low-frequency jobs.`}
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.scheduling_failure_jobs?.items}
            valueFormatter={(value) => `${formatCompact(value)} final scheduling failures`}
            renderMeta={(item) => [
              item.recent_failure_builds?.length ? (
                <span key={`${item.name}-build-links`} className="build-link-list">
                  Latest failed builds:
                  {item.recent_failure_builds.map((build) =>
                    build.build_url ? (
                      <a
                        key={`${item.name}-${build.build_number}`}
                        href={build.build_url}
                        target="_blank"
                        rel="noreferrer"
                        className="ranking-list__link build-link-list__link"
                        title={build.build_url}
                      >
                        #{build.build_number}
                      </a>
                    ) : (
                      <span key={`${item.name}-${build.build_number}`}>
                        #{build.build_number}
                      </span>
                    ),
                  )}
                </span>
              ) : null,
            ]}
          />
        </Panel>

        <Panel
          title="Longest scheduling-wait jobs"
          subtitle={
            schedulingWaitSupported
              ? `Scheduled builds only: average time from pod creation to Scheduled, rolled up to build level; showing only jobs above ${formatSeconds(minSchedulingWaitSeconds)}.`
              : "Requires pod_created_at to measure full scheduling wait from pod creation to Scheduled."
          }
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.scheduling_slowest_jobs?.items}
            valueFormatter={(value) => `${formatOneDecimal(value)}s`}
            renderMeta={(item) => [
              <span key={`${item.name}-samples`}>
                {formatCompact(item.valid_sample_count)} scheduled samples
              </span>,
            ]}
          />
        </Panel>

        <Panel
          title="Image pull failure jobs"
          subtitle="Jobs ranked by builds with image-pull failure evidence such as ErrImagePull or ImagePullBackOff."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.pull_image_failure_jobs?.items}
            valueFormatter={formatCompact}
            renderMeta={(item) => [
              <span key={`${item.name}-attempted`}>
                {formatCompact(item.pull_attempted_count)} pull-attempted builds
              </span>,
              <span key={`${item.name}-avg`}>
                Avg pull {formatSeconds(item.avg_pull_image_s)}
              </span>,
            ]}
          />
        </Panel>

        <Panel
          title="Slowest image pull jobs"
          subtitle="Average image pull duration, using the slowest pod observed for each build."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.pull_image_slowest_jobs?.items}
            valueFormatter={() => ""}
            renderMeta={(item) => [
              <span key={`${item.name}-summary`} className="runtime-meta-summary">
                {formatCompact(item.valid_sample_count)} pull samples avg {formatSeconds(item.value)}
              </span>,
              item.slowest_pull_image ? (
                <span
                  key={`${item.name}-slowest-image`}
                  className="runtime-image-url"
                  title={item.slowest_pull_image}
                >
                  Slowest image: {item.slowest_pull_image}
                </span>
              ) : null,
            ]}
          />
        </Panel>
      </div>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Jenkins Error Catalog Rate"
          loading={page.loading}
          error={page.error}
        >
          <DonutShareChart
            title="Jenkins Error Catalog"
            items={l1Items}
            totalLabel="failures"
            onItemSelect={(item) => setSelectedL1(item.name)}
          />
        </Panel>

        <Panel
          title="Jenkins Error Catalog Trend"
          subtitle={`Showing ${selectedL1} count by time bucket, with its share of the total bucket on the right axis.`}
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={selectedL1Trend.series}
            yFormatter={formatNumber}
            rightYFormatter={formatPercent}
            rightYMin={selectedL1Trend.rightAxis?.min ?? 0}
            rightYMax={selectedL1Trend.rightAxis?.max ?? 10}
          />
        </Panel>

        <Panel
          title={`${selectedL1} Error Details Rate`}
          subtitle="Selected catalog drilldown. INFRA is selected by default because it is the first operational target."
          loading={page.loading}
          error={page.error}
        >
          <DonutShareChart
            title={`${selectedL1} Error Details`}
            subtitle="Error details inside the selected catalog."
            items={selectedL2Share}
            totalLabel="failures"
            emptyMessage="No error details data for the selected catalog."
            onItemSelect={(item) => setSelectedL2(item.name)}
          />
        </Panel>

        <Panel
          title={`${selectedL1} Error Details Trend`}
          subtitle={
            selectedL2
              ? `Showing ${selectedL2} count by time bucket, with its share inside ${selectedL1} on the right axis.`
              : "Select an error detail from the chart on the left."
          }
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={selectedL2Trend.series}
            yFormatter={formatNumber}
            rightYFormatter={formatPercent}
            rightYMin={selectedL2Trend.rightAxis?.min ?? 0}
            rightYMax={selectedL2Trend.rightAxis?.max ?? 10}
          />
        </Panel>

        <Panel
          title={`Top error jobs · ${topJobsDrilldownLabel}`}
          subtitle="Jobs ranked under the active drilldown."
          loading={topErrorJobs.loading}
          error={topErrorJobs.error}
        >
          <RankingList
            items={topErrorJobs.data?.items}
            valueFormatter={formatCompact}
            onItemSelect={(item) => setSelectedJob(item.name)}
            renderMeta={(item) => [
              <span key={`${item.name}-infra`}>
                {formatCompact(item.infra_count)} INFRA-classified failures
              </span>,
            ]}
          />
        </Panel>

        <Panel
          title={`Error builds · ${topJobsDrilldownLabel}${selectedJob ? ` · ${selectedJob}` : ""}`}
          subtitle="Latest 15 builds under current drilldown and selected job, ordered by completion time desc."
          loading={selectedErrorBuilds.loading}
          error={selectedErrorBuilds.error}
          className="panel--error-builds"
        >
          {!selectedJob ? (
            <p className="empty-state empty-state--compact">Select a job from Top error jobs.</p>
          ) : !(selectedErrorBuilds.data?.items || EMPTY_ITEMS).length ? (
            <p className="empty-state empty-state--compact">No builds found for this drilldown.</p>
          ) : (
            <div className="ranking-list error-builds-list">
              {(selectedErrorBuilds.data?.items || EMPTY_ITEMS).map((item, index) => (
                <article key={`${item.build_number}-${item.build_url || item.name}`} className="ranking-list__item">
                  <div className="ranking-list__header">
                    <span className="ranking-list__rank">{String(index + 1).padStart(2, "0")}</span>
                    <div className="ranking-list__title">
                      {item.build_url ? (
                        <a
                          href={item.build_url}
                          target="_blank"
                          rel="noreferrer"
                          className="ranking-list__link"
                          title={item.build_url}
                        >
                          #{item.build_number}
                        </a>
                      ) : (
                        <strong>#{item.build_number}</strong>
                      )}
                    </div>
                  </div>
                  {item.completion_time ? (
                    <div className="ranking-list__meta">
                      <span>Completed {formatBrowserDateTime(item.completion_time)}</span>
                    </div>
                  ) : null}
                </article>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
