# Business Logic Model

## Job 1: `ci-sync-builds`

Source and target:

- source: `prow_jobs`
- target: `ci_l1_builds`

Watermark:

- `{"last_source_prow_row_id": N}`

Execution model:

1. read the current watermark from `ci_job_state`
2. fetch `prow_jobs` rows where `id > last_source_prow_row_id`, ordered by `id`
3. map each source row into one normalized build row
4. bulk upsert by `source_prow_job_id`
5. save progress watermark after each committed batch
6. mark final job status in `ci_job_state`

Per-row mapping:

- copy direct fields
- derive `repo_full_name`
- derive `is_pr_build`
- extract `status` JSON fields
- extract `spec` JSON fields
- compute timing fields
- compute `normalized_build_key`
- compute `cloud_phase`
- initialize V1 derived flags:
  - `is_flaky = 0`
  - `is_retry_loop = 0`
  - `has_flaky_case_match = 0`
  - `failure_category = NULL`

Implementation status:

- implemented in [sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_builds.py)
- covered by sqlite-backed tests in [test_sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/tests/jobs/test_sync_builds.py)

## Job 2: `ci-sync-pr-events`

Source and target:

- source: `github_tickets`
- target: `ci_l1_pr_events`

Watermarks:

- `{"last_ticket_updated_at": T, "last_build_source_prow_row_id_seen": N}`

Candidate PR set:

1. newly seen build-linked PRs from `ci_l1_builds`
2. already tracked PRs whose `github_tickets.updated_at` advanced

Execution model:

1. read both watermarks
2. build candidate PR key set
3. fetch matching `github_tickets` rows
4. for each ticket:
   - emit one `pr_snapshot` row
   - emit `committed` rows
   - emit exact `retest_comment` rows
5. bulk upsert by `(repo, pr_number, event_key)`
6. persist both watermarks

Implementation status:

- planned
- placeholder exists in [sync_pr_events.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_pr_events.py)

## Job 3: `ci-refresh-build-derived`

Source and target:

- source: `ci_l1_builds`, `ci_l1_pr_events`, `problem_case_runs`
- target: `ci_l1_builds`

Watermarks:

- `{"last_processed_build_id": N, "last_processed_pr_event_updated_at": T, "last_processed_case_report_time": T}`

Impacted build set:

- newly inserted or updated builds
- builds touched by new PR events
- builds touched by new case-report rows

Execution phases:

### Phase A: Branch Enrichment

- backfill `target_branch` from `pr_snapshot` rows
- only fill missing `target_branch` values

### Phase B: Flaky Flags

- compute by `(repo_full_name, pr_number, job_name, head_sha)` groups
- reuse pilot logic from `ci_metrics_sample`

### Phase C: Case Evidence

- deterministically recompute `has_flaky_case_match` for impacted builds
- use normalized build URL plus 24-hour report window

### Phase D: Failure Category

- set `FLAKY_TEST` when `is_flaky = 1 OR is_retry_loop = 1`
- else keep `NULL`

Implementation status:

- planned
- placeholder exists in [refresh_build_derived.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/refresh_build_derived.py)

## Shared Execution Conventions

- jobs are idempotent
- jobs write only owned tables
- jobs persist progress through `ci_job_state`
- local unit tests may use sqlite
- production runtime uses TiDB-compatible SQL path
