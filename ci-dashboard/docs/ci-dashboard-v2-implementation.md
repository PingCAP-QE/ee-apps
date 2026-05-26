# CI Dashboard V2 Implementation Plan

Status: Draft v0.4

Last updated: 2026-04-21

Related design docs:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-design.md`
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v2/ci-dashboard/docs/ci-dashboard-v2-pod-question-map.md`

## 1. Implementation Principles

- keep existing V1 APIs and charts stable
- roll out V2 in small, testable slices
- enforce idempotent job behavior and repeatable backfill
- keep source-table access read-only
- do not spend effort on pod charts before real data exists

## 1.1 Current Priority

The current build-out order for V2.1 is:

1. lock Jenkins labels/annotations linkage rules for GCP Jenkins builds
2. finish multi-namespace `sync-pods` ingestion path and schema
3. prepare production rollout/runbook
4. prepare validation plan and validation cases
5. collect real data and review quality
6. only then decide pod API payloads and charts

## 2. Phased Delivery

## 2.1 Phase A: Pod Data Foundation (V2.1)

Deliverables:
1. schema migrations for pod tables
2. lifecycle schema fields for build linkage and filtering:
- `build_system`
- `jenkins_build_url_key`
- `pod_labels_json`
- `pod_annotations_json`
3. pod sync job CLI command
4. job state / watermark support for pod sync
5. one-off backfill support for pod/event windows
6. Cloud Logging reader for pod lifecycle events (primary source)
7. event normalization for `Scheduled/Pulling/Pulled/Created/Started/FailedScheduling`
8. inline Jenkins build-key resolution from pod annotations/labels fetched from Kubernetes API
9. first linkage quality report between pod rows and builds, separated by build system:
- Prow-native
- Jenkins
10. rollout/runbook for CronJob and canary execution
11. validation plan and validation-case matrix

Exit criteria:
- pod sync runs every 10-15 minutes in test/staging
- rerun of same window is idempotent
- sampled Jenkins GCP builds link to the correct build row through pod metadata
- multi-pod Jenkins builds are preserved as multiple pod rows
- linkage coverage report generated
- rollout and recovery steps are documented
- validation cases are reviewable before production execution

## 2.2 Phase B: Pod Analytics API

Deliverables:
1. query helpers for pod-stage decomposition
2. query helpers for pre-ready/post-ready failure split
3. top job ranking query for pod preparation overhead
4. API endpoints integrated into existing page payload
5. nullable support for fields not yet available from first-phase status/spec enrichment

Exit criteria:
- all new endpoints covered by unit tests
- response latency within agreed interactive budget

Note:
- Phase B stays intentionally deferred until Phase A has produced real data worth reviewing.
- Phase A is not complete if it only covers Prow-native pods.

## 2.3 Phase C: Dashboard Panels

Deliverables:
1. `CI Status` panel: build stage decomposition trend
2. `CI Status` panel: pre-ready vs post-ready failure split
3. `CI Status` panel: top pod-overhead jobs

Exit criteria:
- panel behavior works with existing global filters
- axis / label readability validated on broad date ranges

Note:
- chart/UI work is intentionally postponed for now because empty or low-volume pod data is not useful for visual review yet.

## 2.4 Phase D: Validation and Rollout

Deliverables:
1. V2 data-validation report
2. production CronJob and backfill runbook
3. deployment update PR and post-deploy checks

Exit criteria:
- production freshness healthy
- sampled metrics reconcile with raw pod/build records

## 2.5 Phase E: Jenkins Expansion (V2.2)

Deliverables (future phase):
1. Jenkins build metadata ingestion
2. failed build console-log ingestion (scoped)
3. failure-classification enrichment using Jenkins evidence

## 2.6 Phase F: Pod Status/Spec Enrichment (optional extension inside V2.1 or early V2.2)

Deliverables:
1. pod status snapshot enrichment (`initialized_at`, `containers_ready_at`, `ready_at`)
2. pod spec/resource enrichment (`requests`, `limits`)
3. pod-derived failure fields (`oom_killed`, `evicted`, `restart_count`, `termination_reason`)

Exit criteria:
- additional timing fields (`init_seconds`, `container_start_seconds`, `pod_ready_seconds`) become queryable for a meaningful coverage percentage

## 3. Code Change Plan (Pod-first)

## 3.1 SQL / Schema

Add migration files under `sql/` for:
- `ci_l1_pod_lifecycle`
- `ci_l1_pod_spec` (if included in v2.1)
- `ci_l1_pod_events`

Compatibility note:
- the existing experimental migration `sql/009_alter_ci_l1_builds_add_build_system_and_fix_cloud_phase.sql` is not part of the first V2.1 rollout plan
- treat that build-side schema change as a separate compatibility decision for the later combined rollout

## 3.2 Jobs

Expected modules:
- `src/ci_dashboard/jobs/sync_pods.py` (new)
- `src/ci_dashboard/jobs/cli.py` (new command wiring)
- `src/ci_dashboard/jobs/state_store.py` (reuse existing watermark patterns)

Expected ingestion behavior:
- read Cloud Logging `events` log incrementally by time watermark
- cover all agreed CI namespaces, not only `prow-test-pods`
- deduplicate by stable source event identity (`insertId` + project/log scope or event UID)
- map normalized event rows into `ci_l1_pod_events`
- classify pod rows as `PROW_NATIVE` or `JENKINS`
- fetch metadata for touched Jenkins pods from Kubernetes API
- resolve Jenkins build linkage from pod annotations/labels
- derive lifecycle stage timestamps into `ci_l1_pod_lifecycle`
- apply overlap-read window to tolerate logging lag
- resolve lifecycle-to-build linkage through:
  - direct `pod_name` join for Prow-native pods
  - metadata-derived `jenkins_build_url_key` join for Jenkins GCP pods
- preserve one pod row per pod identity; do not collapse multi-pod Jenkins builds during ingestion

Near-term follow-up behavior:
- compute and persist measurable timing fields such as `schedule_wait_seconds`
- leave not-yet-observable fields nullable rather than inferring them unsafely
- preserve pod-derived failure evidence for future `failure_category` enrichment

Operational readiness requirements:
- explicit env contract for GCP project and overlap/lookback controls
- explicit runtime permission to list/get Jenkins agent pods in target namespaces
- CronJob render/apply path consistent with existing V1 jobs
- canary execution path before enabling recurring schedule
- documented expectations for auth on GKE (metadata server / Workload Identity)
- documented default namespace scope for Jenkins and Prow pods

## 3.3 API

Expected modules:
- `src/ci_dashboard/api/queries/pods.py` (new)
- `src/ci_dashboard/api/queries/pages.py` (compose pod blocks)
- `src/ci_dashboard/api/routes/pages.py` and/or dedicated pod routes

First metrics to expose:
- scheduling wait trend
- pod preparation share of total build time
- pre-ready vs post-ready failure split
- top pod-overhead jobs

## 3.4 Frontend

Expected modules:
- `web/src/pages/BuildTrendPage.jsx` (new pod panels in CI Status)
- `web/src/components/charts.jsx` (reuse + extend existing chart primitives)
- `web/src/lib/api.js` (new response mappers/formatters)

## 4. Test Plan

## 4.1 Unit Tests

Backend:
- sync job parsing and mapping logic
- upsert idempotency behavior
- query correctness on fixture datasets
- timing formula correctness for measurable stage fields
- null-handling correctness for fields not yet backfilled

Frontend:
- panel rendering for empty/non-empty states
- formatting and legend behavior for new series

## 4.2 Integration/Smoke

- run migrations on sqlite + TiDB target
- execute: sync-builds -> sync-pr-events -> sync-pods -> refresh-derived
- verify selected build rows end-to-end with pod stage fields
- verify sampled Jenkins GCP pod rows parse to the correct build URL key and build row
- verify sampled multi-pod Jenkins builds are preserved and aggregate correctly
- sample-check pod-derived failure evidence rows (`FailedScheduling`, image pull, restart-related signals) when available

## 4.3 Regression

- existing V1 test suites remain green
- no behavior regression on existing page endpoints without pod data

## 5. Deployment Plan

1. render a suspended `sync-pods` CronJob manifest
2. run one canary Job for pod-event ingestion
3. validate event ingest, multi-namespace coverage, Jenkins metadata fetch, linkage, and lag
4. enable recurring schedule
5. run controlled historical catch-up within logging retention if needed
6. verify freshness before any dashboard/API work continues

Operational notes:
- preserve conservative resource limits first, then tune after first runtime profile
- keep chunk size configurable to avoid large transactions
- no cluster-wide kube-apiserver watch in V2.1 path; prefer Cloud Logging API reads

## 6. Risks and Mitigations

1. Build-Pod linkage mismatch
- mitigation: explicit metadata resolution rules, audit fields, and sampled mismatch review

2. Jenkins metadata drift or RBAC gaps
- mitigation: prefer `buildUrl` / `runUrl`, keep `ci_job + jenkins/label` fallback, add canary validation for pod metadata access, and fail fast on permission errors

3. Pod source retention gaps
- mitigation: 10-15 minute sync cadence, plus explicit lag monitoring

4. Query cost increase
- mitigation: selective denormalization and indexes, with optional derived rollups

5. One-to-many aggregation mistakes
- mitigation: keep storage grain at one pod per row and document build-level aggregation rules explicitly

6. UI complexity growth
- mitigation: add panels incrementally and keep clear scope-separator copy

7. Overpromising timing coverage too early
- mitigation: document metric readiness explicitly and expose nullable / partial coverage honestly until status/spec enrichment lands

## 7. Definition of Done (V2.1)

V2.1 is done when:
1. pod ingestion is running every 10-15 minutes and idempotently in production
2. validation report confirms event integrity, Jenkins metadata-linkage quality, linkage quality, one-to-many aggregation correctness, and acceptable lag
3. runbook documents canary, rerun, catch-up, and failure recovery procedures
4. enough real data exists to start a meaningful pod-UI review
