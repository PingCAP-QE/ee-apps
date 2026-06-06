# CI Dashboard V2 Sync Pods Readiness

Status: Draft v0.3

Last updated: 2026-04-21

Related documents:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-design.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-implementation.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-pod-rollout.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-pod-validation-plan.md`

## 1. Purpose

This document captures what `sync-pods` already does, what assumptions it makes, and what must be true before we enable it as a recurring production job.

This is intentionally about **data readiness**, not chart readiness.

Important boundary:
- this document describes the readiness of the Cloud Logging pod-event half of the pipeline
- by itself, that is not enough to claim V2.1 coverage for Jenkins GCP builds unless Jenkins namespaces and inline pod metadata fetch are in place

## 2. Current Implementation Snapshot

Current code path:
- CLI command: `python -m ci_dashboard.jobs.cli sync-pods`
- module: `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/src/ci_dashboard/jobs/sync_pods.py`
- target tables:
  - `ci_l1_pod_events`
  - `ci_l1_pod_lifecycle`
  - `ci_job_state`

Current behavior:
- reads Cloud Logging `projects/<project>/logs/events`
- filters `resource.type="k8s_pod"`
- filters reasons:
  - `Scheduled`
  - `Pulling`
  - `Pulled`
  - `Created`
  - `Started`
  - `FailedScheduling`
- uses `receiveTimestamp` watermark with overlap reread
- derives one lifecycle row per pod identity
- links lifecycle rows back to `ci_l1_builds` by `pod_name`
- is idempotent at the event level through unique source-event keys

Interpretation:
- the current implementation is structurally sound for direct pod-identity cases
- it is production-meaningful for Prow-native pods
- it is not yet the full V2.1 answer for Jenkins GCP because namespace coverage and pod metadata linkage are not fully wired yet

## 3. Required Runtime Contract

Required environment variables:
- `CI_DASHBOARD_GCP_PROJECT`

Supported tunables:
- `CI_DASHBOARD_BATCH_SIZE`
- `CI_DASHBOARD_POD_EVENT_NAMESPACES`
- `CI_DASHBOARD_POD_SYNC_OVERLAP_MINUTES`
- `CI_DASHBOARD_POD_SYNC_LOOKBACK_MINUTES`
- `CI_DASHBOARD_POD_SYNC_MAX_PAGES`
- `CI_DASHBOARD_LOG_LEVEL`

Database connectivity continues to reuse the existing job contract:
- `CI_DASHBOARD_DB_URL`, or
- `TIDB_HOST` / `TIDB_PORT` / `TIDB_USER` / `TIDB_PASSWORD` / `TIDB_DB` plus optional SSL CA

Authentication expectation on GKE:
- preferred: pod identity / metadata server token
- acceptable in emergency/manual cases: explicit `CI_DASHBOARD_GCP_ACCESS_TOKEN`
- not recommended for recurring production CronJob: long-lived static access tokens

Required GCP permission:
- ability to call Cloud Logging `entries.list` for the target project

Recommended default namespace scope for V2.1:
- `prow-test-pods`
- `jenkins-tidb`
- `jenkins-tiflow`

Observed on 2026-04-21 canary:
- the `apps/ci-dashboard` ServiceAccount metadata token reached Cloud Logging successfully
- but the Logging API returned `403 PERMISSION_DENIED: Permission denied for all log views`
- a manual canary succeeded only after temporarily injecting `CI_DASHBOARD_GCP_ACCESS_TOKEN`
- conclusion: recurring production enablement is still blocked on proper runtime Logging permission

## 4. What Is Production-Ready Now

- schema for raw pod events and derived lifecycle rows exists
- incremental sync with watermark exists
- overlapping reread exists for logging lag tolerance
- event upsert is idempotent
- lifecycle upsert is idempotent
- unit tests cover happy-path ingest and rerun behavior
- CLI wiring is in place
- manual canary proved that real rows can be imported into:
  - `ci_l1_pod_events`
  - `ci_l1_pod_lifecycle`
  - `ci_job_state`

## 4.1 What This Does Not Yet Prove

The 2026-04-21 canary does not yet prove end-to-end V2.1 coverage for Jenkins GCP builds.

Additional validated findings:
- sampled GCP Jenkins builds do not expose the real pod identity through `ci_l1_builds.pod_name`
- sampled Jenkins agent pod labels and annotations already carry enough structure to resolve build linkage directly
- V2.1 should not depend on Jenkins console lookup as the primary readiness path
- multi-pod Jenkins fan-out must be treated as expected behavior

## 5. Known V2.1 Limits We Intentionally Accept

These are acceptable for the first production data round:

1. Source is event-driven, not full pod snapshot driven.
- We can measure the event-derived subset first.
- We do not yet require full pod spec/status snapshots.

2. Lifecycle timing is partial.
- `schedule_to_started_seconds` is available now.
- richer fields like `image_pull_seconds`, `init_seconds`, `containers_ready_at`, `pod_ready_at` stay future work.

3. Build linkage for Jenkins pods must come from labels/annotations, not `pod_name`.
- This is expected to work well for prow build pods.
- It is not sufficient for Jenkins GCP builds.
- We still need explicit validation of metadata-linkage coverage and one-to-many aggregation correctness after Jenkins namespaces are added.

4. Namespace scope is derived from recent build rows.
- In the current prow environment, build rows carry `namespace=apps` while pod events are emitted under `prow-test-pods`.
- V2.1 therefore needs explicit override through `CI_DASHBOARD_POD_EVENT_NAMESPACES`.
- The Phase 1 default should explicitly include Jenkins namespaces rather than relying on discovery from build rows alone.

5. Sync cadence cannot stay loosely hourly if event retention is short.
- Cloud Logging event retention observed during verification was about one hour.
- V2.1 should therefore target a 10-15 minute `sync-pods` cadence.

6. No UI/chart decision is locked yet.
- We first need real data volume and shape.

## 6. Remaining Readiness Gates Before Recurring Production Enablement

## 6.1 Operational Gates

- render script for CronJob exists and is reviewable
- runbook documents canary run, enable, suspend, rerun, and catch-up
- GKE runtime identity for Logging API is not yet confirmed for recurring mode
- DB secret / CA secret contract is confirmed

## 6.2 Data Quality Gates

- `ci_l1_pod_events` receives real pod rows from Cloud Logging
- duplicate source event rate is zero after rerun
- `ci_l1_pod_lifecycle` row count is plausible against distinct pods
- build linkage coverage is measured and acceptable
- Jenkins metadata-linkage coverage is measured and acceptable
- coverage is split by build system (`PROW_NATIVE` vs `JENKINS`)
- lag from log receive time to table freshness is acceptable

Observed on 2026-04-21:
- manual canary inserted real rows and a rerun preserved zero duplicate source identities
- a later `180m` rerun with the metadata-aware Jenkins linkage improved coverage materially
- coverage after that rerun:
  - `PROW_NATIVE`: `232 / 304` = `76.32%`
  - `JENKINS`: `1573 / 2713` = `57.97%`
- Jenkins coverage improved from `1169 / 2428` = `48.15%` on the earlier successful smoke
- remaining coverage gaps were still explainable:
  - `ci_l1_builds` lagged behind pod events by about 37 minutes (`MAX(start_time) = 2026-04-21 09:00:00` while the pod watermark reached `2026-04-21T09:42:37.871069Z`)
  - some pod families such as `dm-it-*` require annotations instead of label-only fallback to link correctly
- linkage quality therefore still needs a post-catch-up recheck before recurring production enablement

## 6.3 Recovery Gates

- failed CronJob leaves prior watermark intact
- rerun of the same window is safe
- controlled catch-up procedure is documented

## 7. Recommended Launch Sequence

1. Apply schema migrations to the target database.
2. Apply a suspended `sync-pods` CronJob.
3. Create one manual pod-event Job from the CronJob template.
4. Validate:
- event rows
- lifecycle rows
- Jenkins namespace coverage
- Jenkins metadata-linkage coverage
- linkage coverage
- lag
5. Enable the recurring schedule only after the smoke run passes.
6. Run a controlled catch-up window if needed.

## 8. Deferred Follow-Ups After First Real Data

- add richer pod timing derivation
- decide whether pod snapshot/spec table is worth adding in V2.1 or V2.2
- decide whether to persist additional failure evidence fields
- design CI Status pod charts based on actual data density and null rate
