import { useEffect, useState } from "react";

import {
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

export default function RuntimeInsightsPage({ filters }) {
  const page = useApiData("/api/v1/pages/runtime-insights", filters);
  const [selectedL1, setSelectedL1] = useState("INFRA");
  const [selectedL2, setSelectedL2] = useState("");
  const [selectedJob, setSelectedJob] = useState("");

  const summary = page.data?.runtime_summary || {};
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
  const selectedL2Share =
    selectedL1 === "INFRA"
      ? page.data?.infra_l2_share?.items || l2Details.INFRA || EMPTY_ITEMS
      : l2Details[selectedL1] || EMPTY_ITEMS;
  const selectedL2Trend = page.data?.error_l2_trends?.items?.[selectedL1]?.series || EMPTY_ITEMS;
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
        eyebrow="Runtime Insights"
        title="Find the runtime bottlenecks hiding between Jenkins and Kubernetes"
        description="An experimental tab for pod scheduling, image pull, and Jenkins error taxonomy signals. Useful panels can graduate into CI Status once the signal proves stable."
        kicker={classificationCoverage.latest_pod_event_at ? `Latest pod event ${classificationCoverage.latest_pod_event_at}` : null}
      />

      <section className="stats-grid">
        <StatCard
          label="Final scheduling failures"
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
              ? "Bars show time from pod creation to Scheduled. The line counts builds whose pods failed to schedule within 30m."
              : "Full scheduling wait needs a pod creation timestamp, so the bar series is unavailable in the current DB. The line still counts builds whose pods failed to schedule within 30m."
          }
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.scheduling_trend?.series}
            yFormatter={formatSeconds}
            rightYFormatter={formatCompact}
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
            rightYAutoPad
          />
        </Panel>

        <Panel
          title="Final scheduling failure jobs"
          subtitle="Jobs ranked by builds whose linked pods failed to schedule within 30m."
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.scheduling_failure_jobs?.items}
            valueFormatter={formatCompact}
            renderMeta={(item) => [
              <span key={`${item.name}-final-failures`}>
                {formatCompact(item.final_failure_count)} final scheduling failures
              </span>,
            ]}
          />
        </Panel>

        <Panel
          title="Longest scheduling-wait jobs"
          subtitle={
            schedulingWaitSupported
              ? "Scheduled builds only: average time from pod creation to Scheduled, rolled up to build level."
              : "Requires pod_created_at to measure full scheduling wait from pod creation to Scheduled."
          }
          loading={page.loading}
          error={page.error}
        >
          <RankingList
            items={page.data?.scheduling_slowest_jobs?.items}
            valueFormatter={formatSeconds}
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
            valueFormatter={formatSeconds}
            renderMeta={(item) => [
              <span key={`${item.name}-samples`}>
                {formatCompact(item.valid_sample_count)} pull samples
              </span>,
            ]}
          />
        </Panel>
      </div>

      <Panel
        title="Image pull failure reasons"
        subtitle="Counts distinct builds by Kubernetes image-pull event reason or message pattern."
        loading={page.loading}
        error={page.error}
      >
        <RankingList
          items={page.data?.pull_image_failure_reasons?.items}
          valueFormatter={formatCompact}
        />
      </Panel>

      <div className="page-grid page-grid--two-column">
        <Panel
          title="Jenkins error L1 share"
          subtitle="Effective category uses human revision first, then machine classification, then OTHERS."
          loading={page.loading}
          error={page.error}
        >
          <DonutShareChart
            title="L1 error category"
            subtitle="Click a category to inspect its L2 breakdown."
            items={l1Items}
            totalLabel="failures"
            onItemSelect={(item) => setSelectedL1(item.name)}
          />
        </Panel>

        <Panel
          title="Jenkins error L1 trend"
          subtitle="Failure-like builds by effective L1 category over time."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={page.data?.error_l1_trend?.series}
            yFormatter={formatNumber}
            stackBars
          />
        </Panel>

        <Panel
          title={`${selectedL1} L2 share`}
          subtitle="Selected L1 category drilldown. INFRA is selected by default because it is the first operational target."
          loading={page.loading}
          error={page.error}
        >
          <DonutShareChart
            title={`${selectedL1} L2 category`}
            subtitle="Effective L2 split inside the selected L1 category."
            items={selectedL2Share}
            totalLabel="failures"
            emptyMessage="No L2 share data for the selected L1 category."
            onItemSelect={(item) => setSelectedL2(item.name)}
          />
        </Panel>

        <Panel
          title={`${selectedL1} L2 trend`}
          subtitle="Trend of the top L2 categories inside the selected L1 bucket."
          loading={page.loading}
          error={page.error}
        >
          <TrendChart
            series={selectedL2Trend}
            yFormatter={formatNumber}
            stackBars
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
                </article>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
