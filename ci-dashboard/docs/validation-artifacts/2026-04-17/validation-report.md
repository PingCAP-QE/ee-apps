# Validation Report

Date: 2026-04-17

Scope of this run:

- database: `insight` on the new TiDB instance
- validation mode: read-only
- executed scope: Phase 0 through Phase 3, excluding Feishu parity and any rerun / idempotency mutation
- reference plan: [data-validation-plan.md](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/data-validation-plan.md)

## Overall Result

The V1 data layer is largely consistent with the documented schema and business rules.

The main blocker found in this round is not a schema or derivation bug. It is an operational freshness gap on the new instance:

- only `ci-sync-flaky-issues` is present in `ci_job_state`
- `ci_l1_builds` is not being kept fresh by recurring jobs yet
- `/api/v1/status/freshness` is therefore technically correct for the current table contents, but operationally incomplete

Everything else validated in this round was either clean or explainable by upstream source-data gaps.

## Pass Summary

### Schema and baseline

- `ci_job_state`, `ci_l1_builds`, `ci_l1_pr_events`, and `ci_l1_flaky_issues` all exist.
- `SHOW CREATE TABLE` matched the checked-in DDL for all four owned tables.
- one-off backfill and flaky-issue sync jobs both completed successfully in K8s.
- `ci_l1_flaky_issues` population looks plausible:
  - `76` total rows
  - `42` `open`, `34` `closed`
  - branch split: `master=66`, `release-8.5=8`, `NULL=2`

### Build ingestion and derivation

- `DV-A03` passed with an explained delta:
  - source rows since `2025-12-01` up to target cutoff: `279,503`
  - target rows: `251,375`
  - delta: `28,128`
  - the delta is fully explained by malformed source rows missing required fields:
    - `21,975` rows with missing `org/repo`
    - `6,159` rows with missing `url`
    - union of invalid required-field rows: `28,128`
- `repo_full_name`, `is_pr_build`, `cloud_phase`, and `source_prow_job_id` uniqueness all had `0` mismatches.
- full-row direct copy check was clean for `namespace`, `job_name`, `org`, `repo`, and `pr_number`.
- timing derivation was clean:
  - `queue_wait_seconds` mismatch: `0`
  - `run_seconds` mismatch: `0`
  - `total_seconds` mismatch: `0`
  - reversed time rows: `0`
- sampled URL normalization matched stored `normalized_build_key` for every inspected GCP sample.

### PR events

- import scope check passed: orphan or unexpected unlinked PR imports = `0`
- every imported PR had exactly one `pr_snapshot`
- event types were limited to:
  - `committed`
  - `pr_snapshot`
  - `retest_comment`
- top repos had `100%` `target_branch` fill rate on `pr_snapshot`
- retest parsing looked clean:
  - total imported `retest_comment` rows: `10,322`
  - invalid non-exact retest bodies after whitespace normalization: `0`

### Derived flaky logic

- branch enrichment mismatch count: `0`
- missing branch fill where snapshot branch existed: none found
- `failure_category` rule was clean:
  - mismatch count: `0`
- `has_flaky_case_match` is populated:
  - matched build rows: `9,416`
  - sampled positive rows all matched real `problem_case_runs` evidence after URL normalization and time-window filtering
- sampled flaky and retry-loop groups behaved consistently with the current algorithm:
  - flaky sample showed failure followed by success on the same `(repo, pr, job, sha)` group
  - retry-loop sample showed repeated failures on the same sha with later attempts flagged as retry loop

### Boundary checks

- orphan PR events: `0`
- future-dated build rows: `0`
- extreme durations exist (`171` rows over 24h), but they are concentrated in long-running integration jobs and do not currently look like a derivation bug

### API and route parity

- `GET /api/v1/filters/repos` matched SQL exactly for the validation window:
  - API repo count: `26`
  - SQL repo count: `26`
- `GET /api/v1/builds/outcome-trend` matched SQL exactly for `repo=pingcap/tidb`, `branch=master`, `2026-03-09` to `2026-04-15`, `granularity=week`
- `GET /api/v1/flaky/top-jobs` top 5 matched SQL exactly under the same scope
- `GET /api/v1/failures/category-trend` matched SQL exactly under the same scope
- `GET /api/v1/flaky/issue-weekly-rates` returned a plausible shape for `repo=pingcap/tidb`, `branch=master`, `issue_status=closed`:
  - `6` weeks
  - `19` case rows
  - included `TestAuditPluginRetrying`
- `GET /api/v1/pages/flaky` correctly ignores `issue_status` for build-level panels:
  - `trend` same for `open` vs `closed`
  - `composition` same for `open` vs `closed`
  - `top_jobs` same for `open` vs `closed`
  - issue-filtered table changed, as expected

## Findings

### 1. Operational freshness is not ready on the new instance

Status: issue

Why this matters:

- dashboard freshness and latest-state correctness are not trustworthy until recurring jobs start running on this instance
- the problem is operational setup, not a schema mismatch

Evidence:

- `ci_job_state` has only one row:
  - `ci-sync-flaky-issues`
- the expected recurring build jobs are missing from `ci_job_state`:
  - `ci-sync-builds`
  - `ci-sync-pr-events`
  - `ci-refresh-build-derived`
- source freshness vs target freshness:
  - `prow_jobs.max(startTime) = 2026-04-17 02:00:25`
  - `ci_l1_builds.max(start_time) = 2026-04-16 14:47:26`
  - lag is about `11h 13m`
- direct source-to-target field comparison found `39` `state` mismatches
  - all sampled mismatches were stale target rows left as `pending`
  - their source rows had already moved to terminal states such as `success`, `aborted`, or `failure`
- `/api/v1/status/freshness` matched SQL exactly, but the SQL itself only contains one job row, so the endpoint is operationally incomplete rather than logically wrong

Likely cause:

- only the one-off `backfill-range` and one-off flaky-issue sync were run on this new instance
- recurring jobs have not yet established their `ci_job_state` rows or begun hourly refresh

Recommended next step:

- run the recurring jobs on the new instance at least once:
  - `ci-sync-builds`
  - `ci-sync-pr-events`
  - `ci-refresh-build-derived`
- then rerun the freshness subset of validation:
  - `DV-A06`
  - `DV-B08`
  - `DV-F01`
  - `DV-F02`

### 2. Five aborted builds have no completion time, but this is an upstream source gap

Status: issue, upstream-source

Why this matters:

- these rows violate the ideal boundary rule that terminal states should have completion timestamps
- they can distort any analysis that assumes all terminal rows have an end timestamp

Evidence:

- `completion_time IS NULL` distribution:
  - `pending = 929`
  - `aborted = 5`
- the five `aborted` rows were traced back to `prow_jobs`
- in all five cases, the source row also had no `completionTime`, and the embedded `status.completionTime` fields were also empty

Impact:

- limited
- these rows are upstream data quality gaps, not a write-path bug in `ci_l1_builds`
- current duration charts use success-only averages, so user-facing impact is low

Recommended next step:

- keep this as a tracked source-data anomaly
- no immediate code change is required unless we want a defensive cleanup or explicit null-handling note in the API

## Observations

- the large build-count delta in `DV-A03` is explained by malformed source rows, not by missing owned-table writes
- `171` builds have `total_seconds > 86400`; current samples are dominated by expected long-running integration jobs, so this is an observation rather than a confirmed issue
- `ci_l1_flaky_issues` has `2` rows with `NULL` branch; this looks plausible for unresolved issue metadata and did not break issue-filtered routes in this run

## Not Run In This Round

- Feishu parity comparison (`DV-E04`) was not executed in this read-only run
- rerun / idempotency validation (`DV-C06`, `DV-D06`, `DV-F03`) was intentionally skipped because the user requested a read-only validation first

## Recommended Next Validation Step

1. initialize or run the three recurring data jobs on the new instance
2. rerun the freshness subset and confirm `ci_job_state` is complete
3. then do the external Feishu parity comparison on the agreed scopes
