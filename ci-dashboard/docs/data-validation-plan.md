# Data Validation Plan

Status: Ready for review, validation not started

Last updated: 2026-04-17

Scope of this plan:

- target database: new TiDB instance, `insight` database
- source tables: `prow_jobs`, `github_tickets`, `problem_case_runs`
- owned tables: `ci_l1_builds`, `ci_l1_pr_events`, `ci_l1_flaky_issues`, `ci_job_state`
- backfill window: `2025-12-01` onward

This document is intentionally a pre-execution review draft.
The goal is to agree on validation cases and execution order before we run the validation itself.

## 1. Current Context

Current runtime status:

- one-off `sync-flaky-issues` job has completed
- large `backfill-range --start-date 2025-12-01` job has completed
- the backfill finished its `refresh_build_derived` phase successfully
- validation execution has not started yet; this document remains a pre-execution review draft

Validation execution can start after review approval.

## 2. Validation Objectives

This validation round focuses on two goals:

1. verify that the data layer matches the V1 design and functional business rules
2. verify that the resulting data is trustworthy enough for dashboard review and pilot-parity comparison

This round does not yet aim to validate:

- pod-event or Jenkins-log collectors
- V2 failure taxonomy
- UI styling details

## 3. Source Design Inputs

The validation cases below are derived from:

- [ci-dashboard-v1-design.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-design.md)
- [ci-dashboard-v1-implementation.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-implementation.md)
- [business-rules.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/functional-design/business-rules.md)
- [business-logic-model.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/functional-design/business-logic-model.md)
- [domain-entities.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/functional-design/domain-entities.md)
- [traceability.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/functional-design/traceability.md)
- prior pilot references under `/Users/dillon/workspace/ci_metrics_sample`

## 4. Validation Principles

- validate from low-level to high-level:
  - object existence
  - row completeness
  - derivation correctness
  - idempotency
  - metric parity
- separate deterministic checks from sample-based checks
- always print scope explicitly for every parity comparison:
  - repo
  - branch
  - time window
  - exclusion policy
  - issue-status policy when applicable
- keep validation evidence queryable and reproducible
- do not start rerun or repair actions until the read-only validation review is complete

## 5. Validation Case Matrix

## 5.1 Group A: Object and Volume Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-A01` | schema presence | required `ci_*` tables exist in `insight` | `SHOW TABLES`, `SHOW CREATE TABLE` | all required tables present |
| `DV-A02` | schema shape | columns, PKs, unique keys, and required indexes match design | `SHOW CREATE TABLE` diff against SQL files | no unexpected missing key/index/column |
| `DV-A03` | source-window build count | `ci_l1_builds` row count is plausible against `prow_jobs` since `2025-12-01` | source/target count comparison | target count is less than or equal to source count and any delta is explained by malformed-source skips |
| `DV-A04` | source-window PR coverage | `ci_l1_pr_events` row count is non-zero and stable for build-linked PRs | target row counts and tracked PR counts | non-zero row count with no obvious collapse |
| `DV-A05` | flaky issue import | `ci_l1_flaky_issues` has expected issue population | row count and branch/status split | non-zero row count; branch distribution plausible |
| `DV-A06` | job state | `ci_job_state` contains expected recurring job records and coherent success timestamps | query `ci_job_state` | recurring jobs are present with coherent status; one-off `backfill-range` completion is verified separately through K8s job state and terminal logs because it does not persist `ci_job_state` |

## 5.2 Group B: Build Ingestion and Field Derivation Validation

| ID | Rule / Design | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-B01` | `BR-01` direct field copy | sample rows match source for namespace, job name, state, repo, PR number | join `prow_jobs` to `ci_l1_builds` on source id / prowJobId | sampled rows match exactly |
| `DV-B02` | `BR-01` repo_full_name | `repo_full_name = CONCAT(org, '/', repo)` | SQL consistency query | zero mismatches |
| `DV-B03` | `BR-01` is_pr_build | `is_pr_build = 1` iff `pr_number IS NOT NULL` | SQL mismatch query | zero mismatches |
| `DV-B04` | `BR-01` timing fields | `queue_wait_seconds`, `run_seconds`, `total_seconds` match timestamp deltas where inputs exist | sampled SQL recomputation | mismatch count is zero or explained by null input fields |
| `DV-B05` | `BR-02` normalized_build_key | URL normalization follows design | sampled URL normalization check across GCP and IDC URLs | sampled outputs match expected normalized path |
| `DV-B06` | `BR-03` cloud_phase | any `prow.tidb.net` URL => `GCP`, otherwise `IDC` | SQL classification check | zero mismatches |
| `DV-B07` | uniqueness | one row per `prowJobId` | group-by duplicate check | zero duplicate `source_prow_job_id` |
| `DV-B08` | freshness shape | newest `ci_l1_builds.start_time` tracks newest source `startTime` within expected lag | max timestamp comparison | lag is acceptable and explainable |

## 5.3 Group C: PR Event Import Validation

| ID | Rule / Design | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-C01` | `BR-05` import scope | only build-linked PRs are imported | anti-join query against unlinked PRs | zero unexpected unlinked imports |
| `DV-C02` | `BR-05` snapshot row | every imported PR has exactly one `pr_snapshot` row | group-by count query | one snapshot per imported PR |
| `DV-C03` | `BR-05` event type scope | imported event types are limited to `pr_snapshot`, `committed`, retest-comment derived rows | distinct `event_type` query | no unexpected event types |
| `DV-C04` | `BR-04` retest exactness | bot instruction comments are not imported as retest events | sample PR timeline review against imported rows | false-positive retest count is zero in samples |
| `DV-C05` | target branch fill | `target_branch` fill rate is measured and plausible | fill-rate query overall and by repo | fill rate is reported; low-fill repos are explainable |
| `DV-C06` | rerun stability | re-running `sync-pr-events` does not inflate event counts | before/after row count + duplicate key check | stable row count, zero duplicate keys |

## 5.4 Group D: Derived Build Logic Validation

| ID | Rule / Design | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-D01` | branch enrichment | `ci_l1_builds.target_branch` is backfilled from `pr_snapshot` where available | join builds to snapshots | no unexplained null branch on build-linked PRs with snapshot branch |
| `DV-D02` | `BR-07` case evidence | `has_flaky_case_match` is set only when repo + normalized URL + report_time window match | sample positive and negative joins | sampled positives and negatives behave as designed |
| `DV-D03` | `BR-08` flaky flags | `is_flaky` agrees with known pilot groups | sample known `(repo, pr, job, sha)` groups | sampled groups match pilot expectation |
| `DV-D04` | `BR-08` retry loop flags | `is_retry_loop` agrees with known retry-loop groups | sample known groups | sampled groups match pilot expectation |
| `DV-D05` | `BR-06` failure category | `failure_category = 'FLAKY_TEST'` iff `is_flaky = 1 OR is_retry_loop = 1` | SQL mismatch query | zero mismatches |
| `DV-D06` | derived idempotency | re-running `refresh-build-derived-range` changes only expected rows | before/after aggregate counts | no unexpected count drift |

## 5.5 Group E: Pilot and Dashboard Metric Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-E01` | build trend base counts | dashboard build counts reconcile to `ci_l1_builds` raw filters | direct SQL vs API output for same scope | counts match |
| `DV-E02` | flaky summary metrics | `flaky`, `blind-retry-loop`, `combined noisy`, `affected PR` rates match SQL logic for a fixed scope | SQL vs API response | values match or differ only by agreed rounding |
| `DV-E03` | issue-filtered weekly case table | selected branch-scoped issue table matches agreed denominator logic | SQL/reference script vs API output | week columns and rates match |
| `DV-E04` | Feishu parity spot-check | agreed scopes for `tidb/master/closed` are compared against reference tables | markdown comparison artifact | differences are either accepted or explicitly categorized |
| `DV-E05` | migration/build trend charts | key build trend panels reconcile to SQL under same filters | SQL vs API response | values match |

### 5.5.1 Route-Level API Checklist

This checklist is meant to make Phase 3 easier to execute without re-deriving the route map during validation.

| Route | Validation Focus | Primary Case Mapping |
| --- | --- | --- |
| `GET /api/v1/filters/repos` | repo list agrees with SQL-distinct repo scope | use during `DV-E01` setup and filter validation |
| `GET /api/v1/filters/branches` | branch list agrees with repo-scoped branch SQL | use during `DV-E01` setup and filter validation |
| `GET /api/v1/builds/outcome-trend` | total / success / failure-like counts match SQL under the exact same scope | `DV-E01` |
| `GET /api/v1/flaky/top-jobs` | flaky / noisy job ranking and counts match SQL under build-level scope | `DV-E02` |
| `GET /api/v1/flaky/issue-weekly-rates` | issue-filtered weekly case rates match SQL / reference script | `DV-E03` |
| `GET /api/v1/failures/category-trend` | `FLAKY_TEST` trend matches SQL under build-level scope | `DV-E05` |
| `GET /api/v1/status/freshness` | freshness payload agrees with `ci_job_state` | `DV-F02` |
| `GET /api/v1/pages/flaky` | assembled flaky page payload is internally coherent across nested panels | Phase 3 end-to-end smoke |

## 5.6 Group F: Operational Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-F01` | job completion visibility | recurring jobs are visible in `ci_job_state`; one-off backfill completion is visible in K8s jobs and logs | `ci_job_state` + `kubectl get jobs` + terminal log tail | states are consistent with job type |
| `DV-F02` | freshness visibility | `/api/v1/status/freshness` and SQL max timestamps are coherent | API response + SQL max timestamp | lag numbers are plausible |
| `DV-F03` | hourly rerun safety | one short rerun does not create duplicates or regress counts | before/after count snapshot | stable totals |

## 5.7 Group G: Data Quality and Boundary Validation

| ID | Target | Validation | Evidence | Pass Criteria |
| --- | --- | --- | --- | --- |
| `DV-G01` | orphan PR events | `ci_l1_pr_events` rows without any matching PR build are measured | left join from PR events to PR builds | zero or explainably small orphan count |
| `DV-G02` | null completion shape | `completion_time IS NULL` rows are distributed only across in-progress states | grouped count by `state` | no unexplained terminal-state rows missing completion time |
| `DV-G03` | future timestamps | `ci_l1_builds.start_time` is not in the future | timestamp sanity query | zero future rows |
| `DV-G04` | extreme durations | unusually large `total_seconds` values are measured and sampled | threshold query + sample rows | outliers are few and explainable |

## 6. Execution Strategy

We should run validation in four phases.

### Phase 0: Freeze and Snapshot

Goal:

- start only after backfill job is fully complete
- capture a stable baseline before any rerun

Prepared steps:

1. confirm K8s job terminal state is `Complete`
2. capture row counts for:
   - `prow_jobs`
   - `github_tickets`
   - `problem_case_runs`
   - `ci_l1_builds`
   - `ci_l1_pr_events`
   - `ci_l1_flaky_issues`
   - `ci_job_state`
3. export `ci_job_state` rows for recurring jobs:
   - `ci-sync-builds`
   - `ci-sync-pr-events`
   - `ci-refresh-build-derived`
   - `ci-sync-flaky-issues`
4. capture K8s terminal state and terminal log tail for any one-off backfill job because `backfill-range` is stateless and does not update `ci_job_state`
5. capture max timestamps from source and target tables

Output:

- one baseline markdown note
- one baseline SQL result artifact

### Phase 1: Deterministic Structural Validation

Run first:

- `DV-A01` through `DV-A06`
- `DV-B02`, `DV-B03`, `DV-B07`
- `DV-C01`, `DV-C02`, `DV-C03`
- `DV-D05`
- `DV-G01`, `DV-G02`, `DV-G03`

Why first:

- these cases are fast
- they catch structural or import-scope mistakes early
- they do not require subjective pilot comparison

### Phase 2: Sample-Based Logic Validation

Run second:

- `DV-B01`, `DV-B04`, `DV-B05`, `DV-B06`
- `DV-C04`, `DV-C05`
- `DV-D01`, `DV-D02`, `DV-D03`, `DV-D04`
- `DV-G04`

Sample design:

- at least 10 sampled build rows:
  - mix of GCP and IDC
  - mix of PR and non-PR
  - mix of success and failure-like
- at least 10 sampled PRs:
  - with commit events
  - with retest comments
  - with bot instruction comments
- at least 5 known pilot groups for flaky / retry-loop verification

Output:

- sample table with `expected`, `actual`, `result`, `notes`

### Phase 3: Metric and Pilot-Parity Validation

Run third:

- `DV-E01` through `DV-E05`
- route checklist under `5.5.1`

Recommended initial scopes:

1. `repo=pingcap/tidb`, `branch=master`, `2026-03-09` to `2026-04-15`
2. `repo=pingcap/tidb`, `branch=release-8.5`, recent 4 to 6 weeks
3. `repo=all`, broad scope smoke only for count sanity

Important:

- lock scope strictly for every comparison
- do not mix branch-filtered and all-branch scopes in one parity result
- do not fill missing weeks with zero unless explicitly required

### Phase 4: Idempotency and Short Rerun Validation

Run last and only after review of earlier results:

- `DV-C06`
- `DV-D06`
- `DV-F03`

Prepared rerun candidates:

- one scoped `sync-pr-events` rerun
- one scoped `refresh-build-derived-range` rerun on a narrow recent window

Why last:

- these cases mutate owned tables again
- we should not do them before baseline and parity evidence is captured

## 7. Execution Checklist

Before validation execution starts:

1. confirm backfill job is complete
2. confirm no new backfill or repair job is running
3. confirm validation scope windows and priority scopes
4. confirm which pilot parity comparisons are mandatory in this round

During execution:

1. run Phase 0 and Phase 1 first
2. review any deterministic failures before continuing
3. run Phase 2 sample checks
4. run Phase 3 metric parity
5. only then decide whether to run Phase 4 rerun/idempotency

After execution:

1. summarize pass/fail by case id
2. separate:
   - code bug
   - source lag / source gap
   - expected design difference
   - parity-decision pending
3. update tracker docs for any confirmed issue

## 7.1 Prepared Execution Steps For Review

This is the proposed runbook once we approve the validation round.

1. Freeze the validation window.
   - record the exact time the validation starts
   - record the agreed parity scopes before any query evidence is collected
2. Capture a baseline snapshot.
   - record row counts for the three source tables and four owned tables
   - record max timestamps and `ci_job_state`
   - save the results as the baseline artifact set
3. Run deterministic structural checks first.
   - start from schema presence, key shape, uniqueness, and import-scope checks
   - stop immediately if these fail, because later parity checks would not be trustworthy
4. Run sampled business-rule checks second.
   - validate field derivation, URL normalization, cloud classification, retest parsing, branch enrichment, and flaky evidence on a curated sample set
   - capture `expected`, `actual`, `result`, and `notes` for every sample
5. Run dashboard metric validation third.
   - compare SQL and API results under exactly the same repo, branch, time range, and issue-status scope
   - keep issue-filtered case metrics separate from job-level dashboard metrics
6. Review all differences before any rerun.
   - classify each difference as code bug, source lag, expected design choice, or parity-decision pending
   - decide whether any difference blocks Phase 4
7. Run idempotency validation last, only if approved.
   - perform a narrow rerun on owned tables only
   - compare before and after counts, duplicates, and derived aggregates
8. Publish the validation outcome.
   - summarize pass/fail by case id
   - link artifact files and any tracker updates

## 7.2 Validation Execution Tracker

Use this checklist table while running the first validation round.

| Step | Phase | Content | Status |
| --- | --- | --- | --- |
| `0` | `0` | freeze baseline, confirm backfill completion, capture row counts, max timestamps, and relevant job state evidence | `done` |
| `1` | `1` | deterministic structural checks for Groups A, B, C, D, and G | `done` |
| `2` | `2` | sample-based logic checks for Groups B, C, D, and G | `done` |
| `3` | `3` | route-level API checks and dashboard metric parity | `done` |
| `4` | `3` | Feishu / pilot parity review for agreed sign-off scopes | `not run in this round` |
| `5` | `4` | rerun / idempotency checks on owned tables only, if approved | `not run in this round` |
| `6` | `post` | summarize findings, classify diffs, and update tracker docs | `done` |

## 8. Execution Assets

### 8.1 Suggested Evidence Artifacts

Suggested artifact set for the validation run:

- `docs/validation-artifacts/<date>/baseline-counts.md`
- `docs/validation-artifacts/<date>/schema-checks.md`
- `docs/validation-artifacts/<date>/sample-build-checks.md`
- `docs/validation-artifacts/<date>/sample-pr-event-checks.md`
- `docs/validation-artifacts/<date>/metric-parity.md`
- `docs/validation-artifacts/<date>/rerun-idempotency.md`

### 8.2 Starter SQL Library

These are starter queries for the first validation round. They are intentionally short and can be copied into a validation artifact with scope notes.

Schema and baseline:

```sql
SHOW TABLES LIKE 'ci_%';

SHOW CREATE TABLE ci_l1_builds;
SHOW CREATE TABLE ci_l1_pr_events;
SHOW CREATE TABLE ci_l1_flaky_issues;
SHOW CREATE TABLE ci_job_state;

SELECT 'prow_jobs' AS table_name, COUNT(*) AS row_count, MIN(startTime) AS min_time, MAX(startTime) AS max_time FROM prow_jobs
UNION ALL
SELECT 'github_tickets', COUNT(*), MIN(created_at), MAX(updated_at) FROM github_tickets
UNION ALL
SELECT 'problem_case_runs', COUNT(*), MIN(report_time), MAX(report_time) FROM problem_case_runs
UNION ALL
SELECT 'ci_l1_builds', COUNT(*), MIN(start_time), MAX(start_time) FROM ci_l1_builds
UNION ALL
SELECT 'ci_l1_pr_events', COUNT(*), MIN(event_time), MAX(event_time) FROM ci_l1_pr_events
UNION ALL
SELECT 'ci_l1_flaky_issues', COUNT(*), MIN(created_at), MAX(updated_at) FROM ci_l1_flaky_issues;

SELECT job_name, last_status, last_succeeded_at, updated_at
FROM ci_job_state
ORDER BY job_name;
```

Build deterministic checks:

```sql
SELECT COUNT(*) AS repo_full_name_mismatch
FROM ci_l1_builds
WHERE repo_full_name <> CONCAT(org, '/', repo);

SELECT COUNT(*) AS is_pr_build_mismatch
FROM ci_l1_builds
WHERE (is_pr_build = 1 AND pr_number IS NULL)
   OR (is_pr_build = 0 AND pr_number IS NOT NULL);

SELECT source_prow_job_id, COUNT(*) AS duplicate_count
FROM ci_l1_builds
GROUP BY source_prow_job_id
HAVING COUNT(*) > 1;

SELECT COUNT(*) AS cloud_phase_mismatch
FROM ci_l1_builds
WHERE (cloud_phase = 'GCP' AND url NOT LIKE 'https://prow.tidb.net/%')
   OR (cloud_phase = 'IDC' AND url LIKE 'https://prow.tidb.net/%');

SELECT MAX(startTime) AS source_max_start_time FROM prow_jobs;
SELECT MAX(start_time) AS target_max_start_time FROM ci_l1_builds;
```

PR event deterministic checks:

```sql
SELECT repo, pr_number, event_key, COUNT(*) AS duplicate_count
FROM ci_l1_pr_events
GROUP BY repo, pr_number, event_key
HAVING COUNT(*) > 1;

SELECT DISTINCT event_type
FROM ci_l1_pr_events
ORDER BY event_type;

SELECT COUNT(*) AS retest_event_mismatch
FROM ci_l1_pr_events
WHERE retest_event = 1 AND event_type <> 'retest_comment';

SELECT repo, pr_number, COUNT(*) AS snapshot_count
FROM ci_l1_pr_events
WHERE event_type = 'pr_snapshot'
GROUP BY repo, pr_number
HAVING COUNT(*) <> 1;
```

Derived and boundary checks:

```sql
SELECT COUNT(*) AS branch_mismatch_count
FROM ci_l1_builds b
JOIN ci_l1_pr_events e
  ON e.repo = b.repo_full_name
 AND e.pr_number = b.pr_number
 AND e.event_type = 'pr_snapshot'
WHERE b.is_pr_build = 1
  AND b.target_branch IS NOT NULL
  AND e.target_branch IS NOT NULL
  AND b.target_branch <> e.target_branch;

SELECT COUNT(*) AS failure_category_mismatch
FROM ci_l1_builds
WHERE ((is_flaky = 1 OR is_retry_loop = 1) AND failure_category <> 'FLAKY_TEST')
   OR ((COALESCE(is_flaky, 0) = 0 AND COALESCE(is_retry_loop, 0) = 0) AND failure_category IS NOT NULL);

SELECT COUNT(*) AS orphan_pr_events
FROM ci_l1_pr_events e
LEFT JOIN ci_l1_builds b
  ON b.repo_full_name = e.repo
 AND b.pr_number = e.pr_number
 AND b.is_pr_build = 1
WHERE b.id IS NULL;

SELECT state, COUNT(*) AS missing_completion_count
FROM ci_l1_builds
WHERE completion_time IS NULL
GROUP BY state
ORDER BY missing_completion_count DESC;

SELECT COUNT(*) AS future_start_time_count
FROM ci_l1_builds
WHERE start_time > UTC_TIMESTAMP();

SELECT COUNT(*) AS extreme_duration_count
FROM ci_l1_builds
WHERE total_seconds > 86400;
```

## 9. Review Questions Before Execution

These are the main decisions to confirm before we start validation:

1. Is this round focused on data-layer correctness first, with dashboard metric parity second?
2. Which parity scopes are mandatory for sign-off:
   - `tidb/master`
   - `tidb/release-8.5`
   - all-repo broad smoke
3. For Feishu comparison differences, should this round judge:
   - current `ci_l1` logic as system of record, or
   - visual parity against legacy pilot outputs?
4. Do we want to run Phase 4 rerun/idempotency in the same validation round, or only after reviewing the first three phases?

## 10. Recommended Starting Set

If we want the smallest useful first round, I recommend this exact execution order:

1. `DV-A01` to `DV-A06`
2. `DV-B02`, `DV-B03`, `DV-B07`
3. `DV-C01`, `DV-C02`, `DV-C03`, `DV-C05`
4. `DV-D01`, `DV-D05`
5. `DV-G01`, `DV-G02`, `DV-G03`
6. `DV-E01`, `DV-E02`
7. `DV-E03` and `DV-E04`
8. route checklist under `5.5.1`
9. review
10. decide whether to continue with sample-deep cases and rerun cases

This gives us:

- structural correctness
- basic logic correctness
- dashboard readiness
- parity signal

without mutating the data again too early.
