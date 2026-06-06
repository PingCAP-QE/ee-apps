# CI Dashboard V2 (Pod First): Question-to-Metric Map

## 1. Context

V1 already provides:
- build-level trend metrics (`success_rate`, `queue/run/total duration`)
- PR-event-derived signals (`flaky`, `blind-retry-loop`, `noisy`)
- issue-linked flaky views

V1 does **not** yet provide:
- Kubernetes pod lifecycle history (scheduling / image pull / container start stages)
- pod-level resource and node context for each CI build

Important coverage constraint:
- for Jenkins GCP builds, these questions only become representative after we collect Jenkins namespaces and resolve build linkage from pod labels and annotations
- `ci_l1_builds.pod_name` alone is not sufficient for those builds

This document defines what new questions become answerable once we add pod data collection in V2.

## 2. Scope of This Document

This is a planning bridge for the V2 design phase:
- focus: `Question -> Metric -> Data Source -> Chart`
- focus on **Pod-first** iteration (before Jenkins log classification)
- output: a prioritized question set that can drive design doc, implementation doc, and acceptance tests

## 3. New Questions We Can Answer With Pod Data

### 3.1 Priority A (first delivery wave)

| ID | Question | Metric(s) | Primary Data Source | Suggested Chart / View |
|---|---|---|---|---|
| POD-Q1 | Are CI slowdowns caused by test runtime or environment preparation? | `queue_wait_s`, `pod_schedule_s`, `image_pull_s`, `container_start_s`, `test_run_s` | `ci_l1_builds` + new `ci_l1_pod_lifecycle` | Stacked trend by week (duration decomposition) |
| POD-Q2 | Are failures concentrated before pod-ready or after test start? | failure counts/rates split by stage: `pre_ready`, `post_ready` | `ci_l1_builds` + `ci_l1_pod_lifecycle` + existing failure signals | Stage-split failure trend + share chart |
| POD-Q3 | Which jobs are most impacted by pod startup overhead? | top jobs by `pod_prepare_ratio = (schedule+pull+start)/total` | `ci_l1_builds` + `ci_l1_pod_lifecycle` | Top-N ranking table + bar |
| POD-Q4 | Is one cluster/node pool causing longer startup or more infra-like failures? | per cluster/pool: p50/p90 pod-ready time, failure-like rate | `ci_l1_pod_lifecycle` (+ optional node labels) + `ci_l1_builds` | Heatmap / grouped trend |
| POD-Q5 | Is migration to GCP improving pod readiness efficiency? | build-side IDC vs GCP: p50/p90 pod-ready time delta over time | `ci_l1_builds` + `ci_l1_pod_lifecycle` | Migration efficiency trend (IDC vs GCP) |

### 3.2 Priority B (second wave after baseline is stable)

| ID | Question | Metric(s) | Primary Data Source | Suggested Chart / View |
|---|---|---|---|---|
| POD-Q6 | Are resource requests over/under-provisioned for key jobs? | request/limit profile vs startup delay and failure-like rate | `ci_l1_pod_spec` + `ci_l1_pod_lifecycle` + `ci_l1_builds` | Scatter + job profile table |
| POD-Q7 | Which scheduling reasons (insufficient cpu/memory/taint/etc.) dominate delays? | reason frequency and impact time | `ci_l1_pod_events` (Pending / Unschedulable reasons) | Pareto by reason |
| POD-Q8 | Are flaky/noisy spikes correlated with pod/environment instability windows? | rolling correlation between noisy rate and pod-ready p90 | existing flaky signals + pod lifecycle aggregate | Dual-axis trend + correlation note |

## 4. Minimal New Data Model (Pod-First)

To answer Priority A, a minimal model can be:

1. `ci_l1_pod_lifecycle` (required)
- key: one row per pod identity
- timestamps: `pod_created_at`, `scheduled_at`, `containers_ready_at`, `pod_ready_at`
- derived: `schedule_s`, `ready_s`, `prepare_s`
- context: `cluster`, `namespace`, `node_name`
- linkage fields: `build_system`, `jenkins_build_url_key`, `source_prow_job_id`, `normalized_build_key`
- note: one build may map to many pod rows for Jenkins fan-out jobs

2. `ci_l1_pod_spec` (recommended)
- key: pod UID / normalized build key mapping
- resource requests/limits: CPU, memory
- useful for Q6, optional for first dashboard wave

3. `ci_l1_pod_events` (required in first wave)
- raw or normalized event reason rows
- needed for Q7 detailed reason Pareto

## 5. What Changes in Dashboard Capability

After Pod-first V2, dashboard can move from:
- "build got slower" (symptom only)

to:
- "build got slower because pod prep stage increased by X%, mostly in pool Y, job group Z" (actionable diagnosis)

After this, Jenkins-log phase can focus on test/code-level failure classification while reusing pod context as environment evidence.

## 6. Recommended V2.1 MVP (Pod-first)

### Must-have
- Jenkins labels/annotations linkage for GCP Jenkins builds
- multi-namespace pod collection including Jenkins namespaces
- Pod lifecycle ingestion job (10-15 minute cadence, idempotent)
- Build-Pod linkage quality checks
- 3 dashboard panels:
  - duration decomposition trend
  - pre-ready vs post-ready failure split
  - top jobs by pod preparation overhead

### Nice-to-have
- cloud/pool comparison panel
- p90 pod-ready SLO card

## 7. Acceptance Questions (for Validation Plan)

When V2.1 is ready, we should be able to answer with data (not inference):

1. In the selected date range, what % of total build time is pod preparation?
2. For failure-like builds, what share failed before pod-ready?
3. Which top 10 jobs lose the most time in pod preparation?
4. Is GCP pod-ready p90 better than IDC in the same week?

## 8. Open Decisions For Next Step

1. Pod source strategy:
- collect from Kubernetes API/event stream
- or rely on persisted pod metadata table (if already available in infra)

2. Build-Pod linkage key:
- whether `normalized_build_key` alone is enough
- whether we also require time-window constraints to avoid collisions

3. Data retention:
- raw events retention window
- aggregate retention policy for long-term trend

4. UI placement:
- add to existing `CI Status` tab vs new dedicated `Runtime Environment` tab
