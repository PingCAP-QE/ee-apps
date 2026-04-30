# CI Dashboard Runtime Insights Design

Status: Draft v0.1

Last updated: 2026-04-29

Reference inputs:
- `ci-dashboard/docs/ci-dashboard-v2-pod-question-map.md`
- `ci-dashboard/docs/ci-dashboard-v2-design.md`
- `ci-dashboard/docs/ci-dashboard-v3-jenkins-design.md`
- `ci-dashboard/sql/005_create_ci_l1_pod_events.sql`
- `ci-dashboard/sql/006_create_ci_l1_pod_lifecycle.sql`
- `ci-dashboard/sql/014_alter_ci_l1_builds_for_v3_jenkins.sql`

## 1. Background

The dashboard now has three evidence layers that can explain CI efficiency and
quality beyond a simple pass/fail trend:

- build-level Jenkins/Prow facts in `ci_l1_builds`
- Kubernetes pod lifecycle and event evidence in `ci_l1_pod_lifecycle` and
  `ci_l1_pod_events`
- Jenkins error classification fields on `ci_l1_builds`

The current `CI Status` page is good for overall build volume, duration, and
success-rate trends. The next step is an experimental diagnosis tab that helps
answer:

- where CI time is spent before tests actually run
- which jobs are most affected by pod scheduling and image pulling
- which failures are likely infra/runtime failures instead of test failures
- whether Jenkins error categories reveal actionable improvement areas

This document defines the first design for that experimental tab.

## 2. Product Positioning

Add a standalone dashboard tab named `Runtime Insights`.

Purpose:
- provide an experimental space for pod, Jenkins, and error-classification
  charts
- validate which charts produce useful CI efficiency and quality insights
- later promote the most valuable charts into the formal `CI Status` tab

Route names:
- frontend route: `/runtime-insights`
- page endpoint: `GET /api/v1/pages/runtime-insights`

Non-goals for the first version:
- no replacement of the existing `CI Status` page
- no requirement that every chart becomes permanent
- no human-review workflow for error classifications
- no deep per-build log viewer

## 3. Key Decisions

### 3.1 Build Is The Primary Analysis Unit

The dashboard should answer CI questions at build/job level. Pod rows are
evidence used to explain a build, not the primary user-facing unit.

Default rule:
- aggregate pod evidence to build level first
- aggregate build-level metrics to week, job, repo, branch, and cloud filters
- expose pod rows only in drill-downs or detail tables

### 3.2 Multiple Pods Under One Build

One Jenkins build can map to multiple pods. Build-level pod metrics use these
rules:

- efficiency metrics use the slowest linked pod for that build
- failure flags are true when any linked pod has matching evidence
- top-job ranking aggregates build-level values, not raw pod rows

This matches the user impact: if any linked pod stalls, the build is affected.

Implementation requirement:
- pod queries must aggregate `ci_l1_pod_lifecycle` rows to build-level rows in a
  CTE or subquery first
- weekly trends, summary cards, and job rankings must aggregate those
  build-level rows, not raw pod rows

### 3.3 Effective Jenkins Error Classification

Jenkins error charts should use effective classification fields:

```sql
COALESCE(revise_error_l1_category, error_l1_category, 'OTHERS')
COALESCE(revise_error_l2_subcategory, error_l2_subcategory, 'UNCLASSIFIED')
```

If a human revision exists, it wins. Otherwise the machine classification is
used. Nulls are shown as `OTHERS / UNCLASSIFIED` so classification coverage is
visible instead of silently disappearing.

### 3.4 Experimental Tab To Formal Tab Promotion

The `Runtime Insights` tab is allowed to contain exploratory charts. A chart can
be promoted to `CI Status` after it proves that it:

- answers a repeated operational question
- has stable data coverage
- has a clear owner or response action
- is understandable without inspecting raw rows

## 4. Data Sources

### 4.1 Build Facts

Primary table:
- `ci_l1_builds`

Important fields:
- `id`
- `repo_full_name`
- `branch`
- `job_name`
- `state`
- `start_time`
- `completion_time`
- `cloud_phase`
- `build_system`
- `source_prow_job_id`
- `normalized_build_url`
- `error_l1_category`
- `error_l2_subcategory`
- `revise_error_l1_category`
- `revise_error_l2_subcategory`

### 4.2 Pod Lifecycle

Primary table:
- `ci_l1_pod_lifecycle`

Important fields:
- `source_project`
- `cluster_name`
- `location`
- `namespace_name`
- `pod_name`
- `pod_uid`
- `build_system`
- `source_prow_job_id`
- `normalized_build_url`
- `repo_full_name`
- `job_name`
- `scheduled_at`
- `first_pulling_at`
- `first_pulled_at`
- `first_created_at`
- `first_started_at`
- `last_failed_scheduling_at`
- `failed_scheduling_count`
- `last_event_at`

### 4.3 Pod Events

Primary table:
- `ci_l1_pod_events`

Important fields:
- `source_project`
- `cluster_name`
- `location`
- `namespace_name`
- `pod_name`
- `pod_uid`
- `event_reason`
- `event_type`
- `event_message`
- `event_timestamp`

This table is required for image-pull failure signals such as
`ImagePullBackOff`, `ErrImagePull`, and `Back-off pulling image`.

## 4.4 Data Validation Findings On 2026-04-29

Production TiDB checks against the current pod dataset found several constraints
that should shape the first implementation:

- current `ci_l1_pod_events` contains only these event reasons in the observed
  window: `Scheduled`, `Pulling`, `Pulled`, `Created`, `Started`,
  `FailedScheduling`
- current `sync-pods` explicitly whitelists those same reasons in
  `POD_EVENT_REASONS`, so `ImagePullBackOff`, `ErrImagePull`, `BackOff`, and
  generic `Failed` events are not currently ingested
- local implementation now expands `POD_EVENT_REASONS` for image-pull failure
  candidates; production still needs the schema migration, deployment, and a new
  sync/backfill window before event-confirmed image-pull failure charts have data
- `first_created_at` in `ci_l1_pod_lifecycle` is derived from the Kubernetes
  `Created` event, which is container creation, not pod object creation
- for Jenkins namespaces, `Created` usually occurs after `Scheduled`, so
  `scheduled_at - first_created_at` is not a valid scheduling-wait metric
- current pod-build linkage is partial: in the observed pod window, 4,491 of
  12,593 build rows had linked pod evidence, or 35.66%
- lifecycle rows with `normalized_build_url` are also partial: 8,620 of 50,912
  pod lifecycle rows
- valid image-pull duration rows are available for 27,244 pod rows, with current
  average pull time around 16.28 seconds
- a scheduling pressure proxy is available today: time from first
  `FailedScheduling` event to eventual `Scheduled`, currently averaging about
  26.19 seconds for pods that had both signals

Design implication:
- true scheduling wait requires adding a real `pod_created_at` timestamp from
  Kubernetes pod metadata or event source data
- local implementation now writes `pod_created_at` from Kubernetes metadata
  `creationTimestamp`; production data needs migration/deployment/backfill before
  this metric is populated historically
- `FailedScheduling` is a scheduler retry warning, not a terminal pod state.
  In an autoscaling GKE cluster, intermediate `Insufficient cpu`,
  `Insufficient memory`, affinity, selector, or taint messages are expected
  during scale-up and should not be shown as CI failures when the pod eventually
  reaches `Scheduled`
- until true scheduling wait is available, P0 scheduling charts should focus on:
  final scheduling failures and successful-retry duration
- implementation note: the first dashboard version uses
  `last_failed_scheduling_at -> scheduled_at` from `ci_l1_pod_lifecycle` for the
  displayed successful-retry duration. A final scheduling failure is currently
  defined as `failed_scheduling_count > 0`, `scheduled_at IS NULL`, and
  `last_event_at` older than a 30-minute grace window. The event-table version
  `first FailedScheduling -> Scheduled` is semantically richer, but it needs a
  materialized lifecycle field or a faster event rollup before it is safe for the
  page-level endpoint on production data.

## 5. Metric Definitions

### 5.1 Build-Pod Linkage

Preferred join:
- `ci_l1_pod_lifecycle.normalized_build_url = ci_l1_builds.normalized_build_url`

Fallback join:
- `ci_l1_pod_lifecycle.source_prow_job_id = ci_l1_builds.source_prow_job_id`

The preferred join should be used whenever both sides have
`normalized_build_url`.

### 5.1.1 Time Difference Helper

Runtime metric queries should not inline dialect-specific timestamp math in each
query. Use a helper with the same semantic order everywhere:

```python
def timediff_seconds_expr(connection, start_col: str, end_col: str) -> str:
    if connection.dialect.name == "sqlite":
        return f"CAST((julianday({end_col}) - julianday({start_col})) * 86400 AS INTEGER)"
    return f"TIMESTAMPDIFF(SECOND, {start_col}, {end_col})"
```

Important:
- `start_col` is always the earlier timestamp
- `end_col` is always the later timestamp
- negative durations should be filtered out before aggregation

### 5.2 Scheduling Metrics

True scheduling wait:

```sql
TIMESTAMPDIFF(SECOND, pod_created_at, scheduled_at)
```

Valid sample:
- `pod_created_at IS NOT NULL`
- `scheduled_at IS NOT NULL`
- `scheduled_at >= pod_created_at`

Build-level metric:
- max valid pod scheduling wait among linked pods

Weekly trend:
- average build-level scheduling wait by week
- only includes builds with at least one successfully scheduled pod

Longest-wait jobs:
- only includes builds with valid `scheduled_at`
- rank jobs by average build-level scheduling wait
- require a minimum sample count, initially `sample_count >= 3`, to avoid noisy
  one-off rankings

Current schema caveat:
- `pod_created_at` does not exist yet
- `first_created_at` must not be used as pod creation time because it represents
  container `Created`

P0 proxy metric:
- failed-scheduling retry span, calculated from raw pod events as:

```sql
TIMESTAMPDIFF(SECOND, first_failed_scheduling_at, scheduled_at)
```

Valid proxy sample:
- `first_failed_scheduling_at IS NOT NULL`
- `scheduled_at IS NOT NULL`
- `scheduled_at >= first_failed_scheduling_at`

Proxy limitation:
- it measures the observable retry span after the first failed scheduling event,
  not total pod-created-to-scheduled wait

Scheduling failure hit:
- `failed_scheduling_count > 0` on any linked pod

Scheduling failure ratio:
- failed-scheduling-hit builds divided by builds with linked pod evidence in the
  same filter scope

### 5.3 Image Pull Time

Pod-level metric:

```sql
TIMESTAMPDIFF(SECOND, first_pulling_at, first_pulled_at)
```

Valid sample:
- `first_pulling_at IS NOT NULL`
- `first_pulled_at IS NOT NULL`
- `first_pulled_at >= first_pulling_at`

Build-level metric:
- max valid image-pull time among linked pods

Weekly trend:
- average build-level image-pull time by week
- only includes builds with completed image-pull evidence

Longest-pull jobs:
- only includes builds with valid `first_pulled_at`
- rank jobs by average build-level image-pull time
- require a minimum sample count, initially `sample_count >= 3`

Image-pull failure hit:
- any linked pod has event evidence where:
  - `event_reason IN ('ImagePullBackOff', 'ErrImagePull')`
  - or `event_message` matches image-pull backoff patterns
  - or lifecycle fallback shows `first_pulling_at IS NOT NULL` and
    `first_pulled_at IS NULL`

Image-pull failure ratio:
- image-pull-failure-hit builds divided by builds with linked pod evidence that
  reached the image-pull stage or has image-pull failure evidence

### 5.4 Pod Preparation Overhead

Initial decomposition:
- scheduling wait: `scheduled_at - pod_created_at` after `pod_created_at` is
  collected
- image pull: `first_pulled_at - first_pulling_at`
- start gap: `first_started_at - scheduled_at`, used only when image-pull
  timing is unavailable or when a broader startup stage is desired

Build-level overhead:
- max pod preparation overhead among linked pods

Overhead ratio:
- `build_pod_prepare_seconds / build_total_duration_seconds`

This chart should be treated as approximate until all stage timestamps are
validated across Jenkins and Prow namespaces.

### 5.5 Pre-Ready vs Post-Ready Failure

Pre-ready failure:
- build is failure-like
- linked pod has scheduling failure, image-pull failure, eviction, OOMKilled, or
  other pod-startup evidence

Post-ready failure:
- build is failure-like
- linked pod evidence shows the pod reached start/ready-like lifecycle points
- no pre-ready failure evidence exists

Unknown:
- build is failure-like
- pod linkage or lifecycle evidence is missing

This split is intended to separate environment failures from test/code failures.

### 5.6 Effective Error Category

L1 category:

```sql
COALESCE(revise_error_l1_category, error_l1_category, 'OTHERS')
```

L2 subcategory:

```sql
COALESCE(revise_error_l2_subcategory, error_l2_subcategory, 'UNCLASSIFIED')
```

Default L1 categories from the current taxonomy:
- `INFRA`
- `BUILD`
- `UT`
- `IT`
- `OTHERS`

Default `INFRA` L2 subcategories:
- `JENKINS`
- `K8S`
- `NETWORK`
- `STORAGE`
- `EXTERNAL_DEP`
- `UNCLASSIFIED`

## 6. Proposed Charts

### 6.1 Page Summary Cards

Purpose:
- quickly show whether the selected window is dominated by runtime delays or
  classified infra failures

Cards:
- average failed-scheduling retry span until `pod_created_at` is available
- scheduling failure ratio
- average image-pull time
- image-pull failure ratio

Each card should show:
- current value
- previous-period delta
- sample count

Keep the first version to four cards so the experimental tab stays scannable.
Infra error share and unclassified share belong in the error-classification and
coverage sections, where their context is visible.

### 6.2 Pod Scheduling Panel

Question:
- are builds waiting too long before pods get scheduled?

Charts:
- weekly average failed-scheduling retry span trend in P0
- weekly average true scheduling wait trend after `pod_created_at` is available
- weekly scheduling failure ratio trend
- top jobs by scheduling failure count
- top jobs by average failed-scheduling retry span in P0
- top jobs by average true scheduling wait after `pod_created_at` is available

Interactions:
- click a job to filter detail tables
- hover trend point to show sample count and p90 when available

### 6.3 Image Pull Panel

Question:
- are image pulls adding meaningful CI delay or causing failures?

Charts:
- weekly average image-pull time trend
- weekly image-pull failure ratio trend
- top jobs by image-pull failure count
- top jobs by average image-pull time
- image-pull failure reason Pareto from pod events

Implementation note:
- event-confirmed image-pull failure charts require expanding `sync-pods`
  ingestion beyond the current six-event whitelist
- until that ingestion change lands, show lifecycle-inferred pull incompletes
  separately from event-confirmed failures

Reason patterns:
- `ImagePullBackOff`
- `ErrImagePull`
- `Back-off pulling image`
- `pull access denied`
- `manifest unknown`
- registry/network timeout phrases

### 6.4 Pod Preparation Decomposition

Question:
- is CI time being spent in runtime preparation or actual test execution?

Charts:
- stacked weekly trend for scheduling, image pull, and startup time
- top jobs by pod preparation overhead ratio
- cloud/namespace comparison if coverage is sufficient

This chart is a bridge between the experimental tab and a future promoted
`CI Status` panel.

Priority:
- P2 until lifecycle timestamp coverage is validated across Jenkins and Prow
  namespaces

### 6.5 Jenkins Error Classification

Question:
- what kinds of Jenkins failures dominate the selected window?

Charts:
- effective L1 category share pie
- effective L1 category weekly trend
- selected L1 drill-down to L2 share
- selected L1 drill-down to L2 weekly trend
- top jobs by selected category count

Default interaction:
- click an L1 pie slice to select that category
- `INFRA` is the default selected L1 when the tab first opens

### 6.6 INFRA Drill-Down

Question:
- which infra subcategories should be investigated first?

Charts:
- `INFRA` L2 subcategory share pie
- `INFRA` L2 subcategory weekly trend
- top jobs by `INFRA` count
- top jobs by `INFRA / K8S`, `INFRA / JENKINS`, or selected L2 count

Expected first-order actionability:
- `K8S`: inspect scheduling, image pull, eviction, OOM, node/pool pressure
- `JENKINS`: inspect remoting, controller, agent disconnects, gateway/backend
- `NETWORK`: inspect transient infra dependency or cluster egress issues
- `STORAGE`: inspect disk, PVC, workspace, controller storage
- `EXTERNAL_DEP`: inspect dependency outages, registry/API rate limits

### 6.7 Classification Coverage

Question:
- can we trust the error taxonomy coverage?

Charts:
- classified vs unclassified weekly trend
- `OTHERS / UNCLASSIFIED` top jobs
- machine vs revised classification count

This panel helps decide whether the AI/rule taxonomy needs more training or
human correction.

Priority:
- P1, because coverage determines whether the classification charts are
  trustworthy enough for promotion to `CI Status`

## 7. Proposed API Shape

The first implementation should use the existing page route pattern:

- `GET /api/v1/pages/runtime-insights`

Suggested response sections:
- `summary`
- `scheduling_trend`
- `scheduling_failure_jobs`
- `scheduling_slowest_jobs`
- `pull_image_trend`
- `pull_image_failure_jobs`
- `pull_image_slowest_jobs`
- `pull_image_failure_reasons`
- `pod_prepare_decomposition`
- `error_l1_share`
- `error_l1_trend`
- `error_l2_details`
- `error_l2_trends`
- `infra_l2_share`
- `infra_l2_trend`
- `error_top_jobs`
- `coverage`

Initial response shape:

```json
{
  "scope": { "...": "CommonFilters.meta()" },
  "summary": {
    "avg_failed_scheduling_retry_span_s": 0,
    "avg_scheduling_wait_s": null,
    "final_scheduling_failure_count": 0,
    "avg_pull_image_s": 0,
    "pull_image_failure_rate_pct": 0,
    "pod_linkage_coverage_pct": 0,
    "valid_scheduling_sample_count": 0,
    "valid_pull_image_sample_count": 0
  },
  "scheduling_trend": { "series": [], "meta": {} },
  "scheduling_failure_jobs": { "items": [] },
  "scheduling_slowest_jobs": { "items": [] },
  "pull_image_trend": { "series": [], "meta": {} },
  "pull_image_failure_jobs": { "items": [] },
  "pull_image_slowest_jobs": { "items": [] },
  "pull_image_failure_reasons": { "items": [] },
  "error_l1_share": { "items": [], "l2_details": {} },
  "error_l1_trend": { "series": [], "meta": {} },
  "error_l2_trends": {},
  "infra_l2_share": { "items": [] },
  "infra_l2_trend": { "series": [], "meta": {} },
  "error_top_jobs": { "items": [] },
  "coverage": {
    "classified_vs_unclassified_trend": { "series": [] },
    "machine_vs_revised": { "groups": [] },
    "latest_pod_event_at": null
  }
}
```

Optional dedicated endpoints after the page stabilizes:
- `GET /api/v1/runtime/pod-scheduling`
- `GET /api/v1/runtime/image-pull`
- `GET /api/v1/runtime/error-categories`
- `GET /api/v1/runtime/infra-breakdown`

## 8. UI Layout

Tab name:
- `Runtime Insights`

Navigation placement:
- add as a standalone sidebar item during experimentation

Frontend implementation files:
- `web/src/pages/RuntimeInsightsPage.jsx`
- `web/src/App.jsx`
- `web/src/components/layout.jsx`
- `web/src/components/charts.jsx` for any new series color keys

Backend implementation files:
- `src/ci_dashboard/api/queries/runtime.py` for pod/runtime queries
- `src/ci_dashboard/api/queries/failures.py` for reusable error-category
  query extensions, unless a separate `errors.py` module becomes clearer during
  implementation
- `src/ci_dashboard/api/queries/pages.py`
- `src/ci_dashboard/api/routes/pages.py`

Suggested page order:
1. summary cards
2. pod scheduling panel
3. image pull panel
4. pod preparation decomposition
5. Jenkins error classification
6. INFRA drill-down
7. classification and linkage coverage

The page should use existing global filters:
- date range
- repo
- branch
- job
- cloud
- bucket

Additional local controls:
- selected error L1 category
- selected error L2 subcategory
- chart mode: count vs ratio for error trends

Component reuse:

| View | Component |
|---|---|
| Summary cards | `StatCard` |
| Scheduling and image-pull trends | `TrendChart` |
| Top jobs | `RankingList` |
| Image-pull failure reasons | `RankingList` or `ShareBars` |
| L1/L2 category share | `DonutShareChart` |
| L1 click-through | `DrilldownModal` |
| Category trends | `TrendChart` with stacked bars |

## 9. Data Quality And Guardrails

Every runtime chart should expose sample quality:

- linked build count
- linked pod count
- pod linkage coverage percentage
- valid duration sample count
- unclassified error percentage
- latest pod event timestamp

Charts should not silently treat missing pod data as zero duration.

Recommended display behavior:
- if sample count is low, show the chart with a low-coverage note
- if linkage coverage is below threshold, show the panel but mark it
  experimental
- if image-pull failure reason extraction is not validated, show the lifecycle
  fallback ratio separately from event-confirmed failures

Initial thresholds:
- top-job rankings require at least 3 valid build-level samples
- low pod-linkage coverage warning starts below 80%
- missing pod data is excluded from duration averages and counted in coverage
  metadata

## 9.1 Performance Notes

Runtime queries should follow the existing API query patterns:

- use `builds_table_expr()` for build-side filtering and TiDB index hints
- use `bucket_expr()` and `filter_complete_week_rows()` for weekly trends
- filter by time window before large joins whenever possible
- aggregate pod rows to build rows before trend or ranking aggregation
- keep page endpoint fan-out behind `_resolve_page_sections()`

Index checks before implementation:
- `ci_l1_pod_lifecycle.normalized_build_url` already has an index and should be
  the primary linkage path
- if TiDB plans scan too broadly, consider an additional scheduled-time index on
  `ci_l1_pod_lifecycle(scheduled_at)`
- validate query plans before adding indexes, because the current table already
  has job/time, repo/time, build-system/time, and normalized-build-url indexes

## 10. Implementation Plan

### Phase 1: Runtime Page Skeleton (P0)

- add `Runtime Insights` route and sidebar item
- add page-level API endpoint returning empty or basic sections
- reuse existing filter behavior

### Phase 2: Pod Scheduling And Image Pull Metrics (P0)

- implement build-level pod aggregation query
- add failed-scheduling retry-span trend, failure ratio, and top jobs
- add image-pull trend, failure ratio, and top jobs
- expand or validate pod event ingestion for image-pull failure reasons
- add `pod_created_at` ingestion before showing true scheduling-wait charts

### Phase 3: Jenkins Classification Views (P0)

- implement effective L1/L2 classification queries
- add L1 share and trend
- add L2 drill-down for selected category
- default to `INFRA`

### Phase 4: INFRA Drill-Down And Coverage (P1)

- add `INFRA` L2 share and trend
- add category-specific top jobs
- add classification coverage and unclassified trend
- add linkage and sample-count visibility

### Phase 5: Experimental Runtime Metrics (P2)

- add pod preparation decomposition
- add pre-ready vs post-ready split after event coverage is validated
- add p90 overlays or hover metadata after average trends are stable

### Phase 6: Promotion Review

- review which charts are actionable
- move selected stable charts into `CI Status`
- keep exploratory or low-coverage charts in `Runtime Insights`

## 10.1 Testing Strategy

Backend tests:
- page endpoint returns all P0 sections
- filters are applied consistently to pod and build queries
- pod metrics aggregate to build level before weekly/job aggregation
- `first_created_at` is not used as pod creation time
- top-job rankings enforce the minimum sample count
- effective category prefers revised fields over machine fields
- null categories become `OTHERS / UNCLASSIFIED`
- SQLite time-difference helper returns positive seconds with the same argument
  order as TiDB

Frontend checks:
- page loads with empty and populated sections
- global filters trigger data refresh
- L1 category click opens the L2 drill-down
- `INFRA` is selected by default
- low-coverage metadata renders without hiding chart data

## 11. Acceptance Questions

The tab is useful if it can answer these questions from dashboard data:

1. Which jobs lose the most time waiting for pod scheduling?
2. Are scheduling failures increasing or decreasing week over week?
3. Which jobs lose the most time pulling images?
4. Are image-pull failures concentrated in specific jobs or namespaces?
5. Are CI failures mostly pre-ready runtime failures or post-ready test failures?
6. Which Jenkins error L1 category dominates the selected window?
7. Inside `INFRA`, which L2 subcategory dominates?
8. How much of the failure population is still unclassified?
9. Which charts are stable and actionable enough to promote into `CI Status`?

## 12. Open Questions

1. Pod creation timestamp:
   - true scheduling wait requires `pod_created_at`
   - current `first_created_at` is container creation and cannot be used

2. Percentiles:
   - weekly trends should start with average for readability
   - p90 should be available in hover or a secondary line after the first
     version

3. Node pool dimension:
   - `cluster_name`, `location`, and `namespace_name` are available
   - node pool is not yet a first-class lifecycle column

4. Image-pull failure extraction:
   - current production ingestion does not include `ImagePullBackOff` or
     `ErrImagePull`
   - expand `POD_EVENT_REASONS` or source event collection before event-confirmed
     image-pull failure charts

5. Pre-ready failure definition:
   - start with scheduling and image-pull evidence
   - later add eviction, OOMKilled, and restart evidence after validating event
     coverage
