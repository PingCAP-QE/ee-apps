# CI Dashboard V1 Implementation Spec

Status: Draft v0.1

Last updated: 2026-04-13

Companion design document:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/ci-dashboard-v1-design.md`

Deployment design document:
- `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/docs/deploy-design.md`

## 1. Purpose

This document translates the V1 design into concrete implementation choices:

- repository layout
- database schema
- job watermarks and execution flow
- API route surface
- dashboard app packaging
- Kubernetes deployment layout

This document is intentionally implementation-oriented. If this document conflicts with the design document, the design document should be updated rather than silently diverging in code.

## 2. Implementation Boundaries

V1 implementation must respect the following constraints:

- upstream source tables remain read-only:
  - `prow_jobs`
  - `github_tickets`
  - `problem_case_runs`
- V1 owned tables:
  - `ci_l1_builds`
  - `ci_l1_pr_events`
  - `ci_job_state`
- V1 does not create:
  - `ci_raw_*`
  - physical `ci_agg_*`
  - `ci_l1_test_case_runs`
- V1 dashboard app is packaged as:
  - FastAPI backend
  - React frontend
  - one deployable app
- V1 data jobs stay independent from the dashboard app
- raw build ingestion remains all-repo and all-branch
- credentials and secrets must not be baked into source code, Docker images, or committed deployment values
- runtime credentials must be injected from Kubernetes Secret objects
- `github_tickets` is a best-effort enrichment source:
  - missing rows must not fail `ci-sync-pr-events`
  - missing rows may leave `target_branch` and related PR metadata null
- PR-event completeness for `pingcap/docs`, `pingcap/docs-cn`, and `PingCAP-QE/ci` is out of scope for V1
- short `github_tickets` lag for `pingcap/tidb` and `pingcap/ticdc` is accepted in V1

## 3. Proposed Repository Layout

Proposed new top-level module:

```text
ee-apps/
  ci-dashboard/
    pyproject.toml
    Makefile
    Dockerfile.app
    Dockerfile.jobs
    README.md
    src/
      ci_dashboard/
        common/
          config.py
          db.py
          logging.py
          models.py
          sql_helpers.py
        api/
          main.py
          deps.py
          schemas/
          routes/
            filters.py
            flaky.py
            builds.py
            failures.py
          queries/
            filters.py
            flaky.py
            builds.py
            failures.py
          static/
        jobs/
          cli.py
          sync_builds.py
          sync_pr_events.py
          refresh_build_derived.py
          state_store.py
          retest_parser.py
          build_url_matcher.py
    web/
      package.json
      src/
        app/
        pages/
        components/
        api/
        hooks/
        styles/
      public/
    sql/
      001_create_ci_l1_builds.sql
      002_create_ci_l1_pr_events.sql
      003_create_ci_job_state.sql
    tests/
      api/
      jobs/
      sql/
```

Why this layout:

- one Python package for shared config, DB utilities, and business rules
- API and jobs can share model definitions and SQL helper code
- React frontend is isolated but packaged into the app image at build time
- SQL migrations stay explicit and reviewable
- component ownership stays clear even though API and jobs reuse one Python package:
  - `api/` owns HTTP contracts
  - `jobs/` owns incremental sync and derived-field refresh
  - `common/` owns shared config, DB access, and reusable parsing helpers

## 4. Runtime Components

### 4.1 Dashboard App

One application image containing:

- FastAPI backend
- built React assets

Runtime behavior:

- `/api/*` is handled by FastAPI
- `/healthz`, `/livez`, `/readyz` are served by FastAPI
- all non-API routes serve the React SPA entrypoint

### 4.2 Data Jobs

One jobs image containing three executable commands:

- `python -m ci_dashboard.jobs.cli sync-builds`
- `python -m ci_dashboard.jobs.cli sync-pr-events`
- `python -m ci_dashboard.jobs.cli refresh-build-derived`

Each command is scheduled independently by CronJob resources.

### 4.3 Runtime Interaction Rules

Request path for the dashboard app:

```text
Browser -> React SPA -> FastAPI -> TiDB
```

Coordination rules:

- jobs and dashboard app do not call each other directly
- jobs coordinate only through `ci_l1_builds`, `ci_l1_pr_events`, and `ci_job_state`
- frontend never connects to TiDB directly
- V1 uses no message queue and no separate orchestration service

## 5. Database Objects

## 5.1 `ci_l1_builds`

Purpose:

- normalized build fact table

Grain:

- one row per source `prow_jobs.prowJobId`

DDL proposal:

```sql
CREATE TABLE ci_l1_builds (
  id BIGINT NOT NULL AUTO_INCREMENT,
  source_prow_row_id BIGINT NOT NULL,
  source_prow_job_id CHAR(36) NOT NULL,
  namespace VARCHAR(255) NOT NULL,
  job_name VARCHAR(255) NOT NULL,
  job_type VARCHAR(32) NOT NULL,
  state VARCHAR(32) NOT NULL,
  optional TINYINT(1) NOT NULL DEFAULT 0,
  report TINYINT(1) NOT NULL DEFAULT 0,
  org VARCHAR(63) NOT NULL,
  repo VARCHAR(63) NOT NULL,
  repo_full_name VARCHAR(127) NOT NULL,
  base_ref VARCHAR(255) NULL,
  pr_number BIGINT NULL,
  is_pr_build TINYINT(1) NOT NULL DEFAULT 0,
  context VARCHAR(255) NULL,
  url VARCHAR(1024) NOT NULL,
  normalized_build_key VARCHAR(1024) NULL,
  author VARCHAR(255) NULL,
  retest TINYINT(1) NULL,
  event_guid VARCHAR(255) NULL,
  build_id VARCHAR(64) NULL,
  pod_name VARCHAR(255) NULL,
  pending_time DATETIME NULL,
  start_time DATETIME NOT NULL,
  completion_time DATETIME NULL,
  queue_wait_seconds INT NULL,
  run_seconds INT NULL,
  total_seconds INT NULL,
  head_sha CHAR(40) NULL,
  target_branch VARCHAR(255) NULL,
  cloud_phase VARCHAR(16) NOT NULL DEFAULT 'IDC',
  is_flaky TINYINT(1) NOT NULL DEFAULT 0,
  is_retry_loop TINYINT(1) NOT NULL DEFAULT 0,
  has_flaky_case_match TINYINT(1) NOT NULL DEFAULT 0,
  failure_category VARCHAR(32) NULL,
  failure_subcategory VARCHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ci_l1_builds_source_prow_job_id (source_prow_job_id),
  KEY idx_ci_l1_builds_repo_time (repo_full_name, start_time),
  KEY idx_ci_l1_builds_repo_branch_time (repo_full_name, target_branch, start_time),
  KEY idx_ci_l1_builds_repo_pr_job_sha (repo_full_name, pr_number, job_name, head_sha, start_time),
  KEY idx_ci_l1_builds_normalized_build_key (normalized_build_key)
);
```

Field derivation rules:

- `repo_full_name = CONCAT(org, '/', repo)`
- `is_pr_build = 1` when `pr_number IS NOT NULL`
- `normalized_build_key` uses the same normalization rule already validated in pilot work:
  - remove `https://do.pingcap.net`
  - remove `https://prow.tidb.net`
  - remove `/display/redirect`
  - trim trailing `/`
- `head_sha` comes from `spec.refs.pulls[0].sha` when present
- `pending_time`, `build_id`, `pod_name` come from `status`
- `start_time` uses source `startTime`
- `completion_time` uses source `completionTime`
- `queue_wait_seconds = pending_time - start_time` when both exist
- `run_seconds = completion_time - pending_time` when both exist
- `total_seconds = completion_time - start_time` when both exist
- `cloud_phase`:
  - `GCP` when `url` starts with `https://prow.tidb.net/jenkins/`
  - `IDC` otherwise
- `failure_category`:
  - `FLAKY_TEST` when `is_flaky = 1 OR is_retry_loop = 1`
  - `NULL` otherwise in V1

## 5.2 `ci_l1_pr_events`

Purpose:

- normalized PR event and PR snapshot table for retest and target-branch enrichment

Grain:

- one row per imported timeline event or synthetic PR snapshot row

DDL proposal:

```sql
CREATE TABLE ci_l1_pr_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  repo VARCHAR(255) NOT NULL,
  pr_number BIGINT NOT NULL,
  event_key VARCHAR(128) NOT NULL,
  event_time DATETIME NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  actor_login VARCHAR(255) NULL,
  comment_id BIGINT NULL,
  comment_body TEXT NULL,
  retest_event TINYINT(1) NOT NULL DEFAULT 0,
  commit_sha CHAR(64) NULL,
  target_branch VARCHAR(255) NULL,
  head_ref VARCHAR(255) NULL,
  head_sha CHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ci_l1_pr_events_repo_pr_event_key (repo, pr_number, event_key),
  KEY idx_ci_l1_pr_events_repo_pr_time (repo, pr_number, event_time),
  KEY idx_ci_l1_pr_events_repo_branch_time (repo, target_branch, event_time),
  KEY idx_ci_l1_pr_events_commit_sha (commit_sha)
);
```

Import rules:

- import only build-linked PRs
- materialize one synthetic `pr_snapshot` row per imported PR so branch metadata is always present even when no selected timeline event exists
- import timeline events only for:
  - `committed`
  - exact retest command comments
- use `branches.base.ref` as `target_branch`
- use `branches.head.ref` and `branches.head.sha` when available

Event key rules:

- PR snapshot row:
  - fixed key `pr_snapshot`
  - upsert on each imported PR refresh
- comment event:
  - `retest_comment:{comment_id}` when `comment_id` exists
  - otherwise deterministic hash fallback
- commit event:
  - `committed:{commit_sha}:{event_time}`
  - if `commit_sha` is absent, use deterministic hash fallback

## 5.3 `ci_job_state`

Purpose:

- persist job watermarks and recent run state

DDL proposal:

```sql
CREATE TABLE ci_job_state (
  job_name VARCHAR(64) NOT NULL,
  watermark_json JSON NOT NULL,
  last_started_at DATETIME NULL,
  last_succeeded_at DATETIME NULL,
  last_status VARCHAR(16) NOT NULL DEFAULT 'never',
  last_error TEXT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (job_name)
);
```

Recommended `job_name` values:

- `ci-sync-builds`
- `ci-sync-pr-events`
- `ci-refresh-build-derived`

Watermark shape examples:

```json
{"last_source_prow_row_id": 12345678}
```

```json
{"last_ticket_updated_at": "2026-04-13T10:00:00Z", "last_build_source_prow_row_id_seen": 12345678}
```

```json
{"last_processed_build_id": 987654, "last_processed_pr_event_updated_at": "2026-04-13T10:00:00Z", "last_processed_case_report_time": "2026-04-13T10:00:00Z"}
```

## 6. Shared Parsing Rules

## 6.1 Exact Retest Parsing

Supported commands in V1:

- `/retest`
- `/retest-required`

Normalization rule:

- trim leading and trailing whitespace
- collapse repeated whitespace
- compare exact normalized body

Accepted regex:

```text
^/(retest|retest-required)$
```

Implementation note:

- use explicit exact-command parsing rather than substring matching
- bot comments like `say /retest to rerun ...` must not be classified as retest events

Recommended exact parser:

```python
def normalize_command(body: str) -> str:
    return " ".join(body.strip().split())

def is_supported_retest_command(body: str) -> bool:
    normalized = normalize_command(body)
    return normalized in {"/retest", "/retest-required"}
```

## 6.2 Build URL Normalization

Use one shared helper in both jobs and API code.

Normalization steps:

1. remove prefix `https://do.pingcap.net`
2. remove prefix `https://prow.tidb.net`
3. remove suffix `/display/redirect`
4. trim trailing `/`

Example:

- input: `https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect`
- normalized key: `/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299`

## 7. Job Implementation

## 7.1 `ci-sync-builds`

Input:

- `prow_jobs`
- `ci_job_state`

Output:

- upsert into `ci_l1_builds`
- update `ci_job_state(job_name='ci-sync-builds')`

Watermark:

- `last_source_prow_row_id`

Algorithm:

1. read watermark from `ci_job_state`
2. fetch `prow_jobs` rows where `id > :last_source_prow_row_id`
3. process in ascending `id` batches
4. for each row:
   - extract derived fields
   - compute `normalized_build_key`
   - compute `cloud_phase`
   - initialize:
     - `is_flaky = 0`
     - `is_retry_loop = 0`
     - `has_flaky_case_match = 0`
     - `failure_category = NULL`
5. bulk upsert into `ci_l1_builds`
6. commit
7. update watermark to last processed source row id

Upsert key:

- `source_prow_job_id`

Pseudo-SQL fetch:

```sql
SELECT *
FROM prow_jobs
WHERE id > :last_source_prow_row_id
ORDER BY id
LIMIT :batch_size
```

## 7.2 `ci-sync-pr-events`

Input:

- `github_tickets`
- `ci_l1_builds`
- `ci_job_state`

Output:

- upsert into `ci_l1_pr_events`
- update `ci_job_state(job_name='ci-sync-pr-events')`

Watermarks:

- `last_ticket_updated_at`
- `last_build_source_prow_row_id_seen`

Why two watermarks are needed:

- new build-linked PRs may need import even if their GitHub ticket was last updated before the current ticket watermark
- already-known build-linked PRs need refresh when `github_tickets.updated_at` changes

Candidate PR set:

1. newly seen build-linked PRs from `ci_l1_builds` where `source_prow_row_id > last_build_source_prow_row_id_seen`
2. already-known build-linked PRs whose `github_tickets.updated_at > last_ticket_updated_at`

Accepted source limitations:

- if a build-linked PR has no matching `github_tickets` row, the job skips PR-event materialization for that PR and continues
- this is expected for out-of-scope repos such as `pingcap/docs`, `pingcap/docs-cn`, and `PingCAP-QE/ci`
- short upstream lag on `github_tickets` is also acceptable; later runs can backfill missing PR events once source rows appear

Algorithm:

1. read job state
2. build candidate PR key set
3. fetch corresponding `github_tickets` rows in batches
4. parse:
   - one synthetic `pr_snapshot` row with latest branch metadata
   - exact retest comments
   - commit events
5. bulk upsert into `ci_l1_pr_events`
6. update:
   - `last_ticket_updated_at` to max processed `github_tickets.updated_at`
   - `last_build_source_prow_row_id_seen` to max build row seen during candidate selection

Pseudo-query for newly seen build-linked PRs:

```sql
SELECT DISTINCT repo_full_name, pr_number
FROM ci_l1_builds
WHERE is_pr_build = 1
  AND source_prow_row_id > :last_build_source_prow_row_id_seen
```

Pseudo-query for updated tracked PRs:

```sql
SELECT DISTINCT g.repo, g.number
FROM github_tickets g
JOIN (
  SELECT DISTINCT repo, pr_number
  FROM ci_l1_pr_events
) tracked
  ON tracked.repo = g.repo
 AND tracked.pr_number = g.number
WHERE g.type = 'pull'
  AND g.updated_at > :last_ticket_updated_at
```

## 7.3 `ci-refresh-build-derived`

Input:

- `ci_l1_builds`
- `ci_l1_pr_events`
- `problem_case_runs`
- `ci_job_state`

Output:

- update derived fields in `ci_l1_builds`
- update `ci_job_state(job_name='ci-refresh-build-derived')`

Responsibilities:

- backfill `target_branch`
- recompute `is_flaky`
- recompute `is_retry_loop`
- recompute `has_flaky_case_match`
- recompute `cloud_phase`
- recompute `failure_category`

Watermarks:

- `last_processed_build_id`
- `last_processed_pr_event_updated_at`
- `last_processed_case_report_time`

Refresh strategy:

Derive `impacted_build_ids` as the union of:

- builds inserted or updated since `last_processed_build_id`
- builds whose `(repo_full_name, pr_number)` pair is touched by `ci_l1_pr_events.updated_at > last_processed_pr_event_updated_at`
- builds matched by newly seen `problem_case_runs.report_time > last_processed_case_report_time`

### Phase A: branch enrichment

Update builds with authoritative PR snapshot data:

```sql
UPDATE ci_l1_builds b
JOIN (
  SELECT repo, pr_number, target_branch
  FROM ci_l1_pr_events
  WHERE event_type = 'pr_snapshot'
    AND target_branch IS NOT NULL
) e
  ON e.repo = b.repo_full_name
 AND e.pr_number = b.pr_number
SET b.target_branch = e.target_branch
WHERE b.is_pr_build = 1
  AND (b.target_branch IS NULL OR b.target_branch = '')
```

### Phase B: build-level flaky flags

Implementation requirement:

- reuse the pilot grouping logic already validated in `ci_metrics_sample`
- compute on impacted groups only

Impacted groups:

- rows in `impacted_build_ids`
- groups touched by new `committed` or `retest_comment` rows since `last_processed_pr_event_updated_at`

Group key:

- `(repo_full_name, pr_number, job_name, head_sha)`

Writeback:

- update `is_flaky`
- update `is_retry_loop`

### Phase C: case-level evidence

Set `has_flaky_case_match = 1` when:

- `problem_case_runs.flaky = 1`
- `problem_case_runs.repo = ci_l1_builds.repo_full_name`
- normalized `problem_case_runs.build_url = ci_l1_builds.normalized_build_key`
- `problem_case_runs.report_time` is between:
  - `ci_l1_builds.start_time`
  - `ci_l1_builds.start_time + INTERVAL 24 HOUR`

Recommended update shape:

```sql
UPDATE ci_l1_builds b
LEFT JOIN (
  SELECT DISTINCT
    b2.id AS build_id
  FROM ci_l1_builds b2
  JOIN problem_case_runs p
    ON p.flaky = 1
   AND p.repo = b2.repo_full_name
   AND TRIM(TRAILING '/' FROM REPLACE(REPLACE(REPLACE(p.build_url,
      'https://do.pingcap.net', ''),
      'https://prow.tidb.net', ''),
      '/display/redirect', '')) = b2.normalized_build_key
   AND p.report_time BETWEEN b2.start_time AND b2.start_time + INTERVAL 24 HOUR
  WHERE b2.id IN (:impacted_build_ids)
) f
  ON f.build_id = b.id
SET b.has_flaky_case_match = CASE WHEN f.build_id IS NOT NULL THEN 1 ELSE 0 END
WHERE b.id IN (:impacted_build_ids)
```

Implementation note:

- use the same URL normalization rule here as in Section 6.2
- recompute deterministically for the impacted build set rather than only setting rows from `0 -> 1`

### Phase D: failure category

V1 rule:

```sql
UPDATE ci_l1_builds
SET failure_category = CASE
  WHEN is_flaky = 1 OR is_retry_loop = 1 THEN 'FLAKY_TEST'
  ELSE NULL
END
WHERE id IN (:impacted_ids)
```

V1 explicitly does not assign:

- `INFRA`
- `CODE_DEFECT`

without future pod or Jenkins evidence.

## 7.4 Recommended Job Module Boundaries

The following internal function boundaries are recommended so each job remains testable and easy to backfill independently.

### `sync_builds.py`

| Function | Purpose | Notes |
| --- | --- | --- |
| `load_watermark()` | Read `last_source_prow_row_id` from `ci_job_state` | Default to `0` on first run |
| `fetch_source_rows(after_id, batch_size)` | Fetch `prow_jobs` rows in ascending source order | Use batched incremental scans |
| `extract_status_fields(row)` | Parse `status` JSON into `pending_time`, `build_id`, `pod_name` | Must tolerate malformed or partial JSON |
| `extract_spec_fields(row)` | Parse `spec` JSON into `head_sha` and PR refs | Must tolerate non-PR builds |
| `map_build_row(row)` | Produce one normalized `ci_l1_builds` row | Includes `normalized_build_key` and `cloud_phase` |
| `upsert_build_batch(rows)` | Bulk upsert one build batch | Keyed by `source_prow_job_id` |
| `save_watermark(last_id)` | Persist successful progress to `ci_job_state` | Only after batch commit |
| `run_once()` | Orchestrate one job execution | CLI entry point calls this |

### `sync_pr_events.py`

| Function | Purpose | Notes |
| --- | --- | --- |
| `load_watermarks()` | Read `last_ticket_updated_at` and `last_build_source_prow_row_id_seen` | Default to empty state on first run |
| `collect_candidate_prs()` | Build the tracked PR key set for this run | Union new build-linked PRs and updated tracked PRs |
| `fetch_ticket_rows(pr_keys)` | Read matching `github_tickets` rows | Batched by `(repo, number)` |
| `build_pr_snapshot_row(ticket)` | Materialize one synthetic `pr_snapshot` row per PR | Carries `target_branch`, `head_ref`, `head_sha` |
| `extract_commit_events(ticket)` | Parse `committed` events into normalized rows | Stable `event_key` generation required |
| `extract_retest_events(ticket)` | Parse exact retest comments into normalized rows | Must use exact command logic only |
| `upsert_pr_event_batch(rows)` | Bulk upsert snapshot and timeline rows | Keyed by `(repo, pr_number, event_key)` |
| `save_watermarks(state)` | Persist new ticket/build watermarks | Only after successful writes |
| `run_once()` | Orchestrate one job execution | CLI entry point calls this |

### `refresh_build_derived.py`

| Function | Purpose | Notes |
| --- | --- | --- |
| `load_watermarks()` | Read build, PR-event, and case-report watermarks | Used to derive the impacted build set |
| `collect_impacted_build_ids()` | Compute the minimal build set needing recomputation | Union builds touched by new builds, PR events, or case rows |
| `backfill_target_branch(build_ids)` | Fill missing `target_branch` from `pr_snapshot` rows | Do not overwrite already-populated values |
| `recompute_flaky_flags(build_ids)` | Re-run pilot grouping logic on impacted groups | Writes `is_flaky` and `is_retry_loop` |
| `recompute_flaky_case_match(build_ids)` | Deterministically recompute `has_flaky_case_match` | Uses normalized build URL plus 24h window |
| `recompute_cloud_phase(build_ids)` | Re-apply URL-prefix classification | Cheap and deterministic |
| `recompute_failure_category(build_ids)` | Set `FLAKY_TEST` or `NULL` | No `INFRA` or `CODE_DEFECT` in V1 |
| `save_watermarks(state)` | Persist successful recomputation progress | Done after all write phases commit |
| `run_once()` | Orchestrate one job execution | CLI entry point calls this |

### Shared Helpers

| Module | Responsibility |
| --- | --- |
| `state_store.py` | `ci_job_state` read/write helpers |
| `retest_parser.py` | exact command normalization and retest detection |
| `build_url_matcher.py` | shared build URL normalization |
| `common/db.py` | engine/session creation and retry-safe transaction helpers |

## 8. API Implementation

Backend framework:

- FastAPI
- SQLAlchemy Core or text-query style against TiDB

Recommendation:

- keep queries explicit rather than introducing a heavy ORM abstraction for analytics SQL

## 8.1 API Route Layout

```text
/healthz
/livez
/readyz
/api/v1/status/freshness
/api/v1/filters/repos
/api/v1/filters/branches
/api/v1/filters/jobs
/api/v1/filters/cloud-phases
/api/v1/flaky/trend
/api/v1/flaky/composition
/api/v1/flaky/top-jobs
/api/v1/flaky/period-comparison
/api/v1/builds/outcome-trend
/api/v1/builds/duration-trend
/api/v1/builds/cloud-comparison
/api/v1/failures/category-trend
/api/v1/failures/category-share
```

## 8.2 Shared Query Parameters

Recommended common filter model:

- `repo: str | None`
- `branch: str | None`
- `job_name: str | None`
- `cloud_phase: str | None`
- `start_date: date | None`
- `end_date: date | None`
- `granularity: Literal["day", "week"] = "day"`

Behavior:

- omitted filters mean "all"
- branch filtering should use:
  - `target_branch` for PR-aware views
  - `base_ref` only as a fallback when necessary

## 8.3 Response Shape Conventions

Use chart-friendly responses, not raw table dumps.

Example filter response:

```json
{
  "items": [
    {"value": "pingcap/tidb", "label": "pingcap/tidb"}
  ]
}
```

Example freshness response:

```json
{
  "jobs": [
    {
      "job_name": "ci-sync-builds",
      "last_status": "succeeded",
      "last_succeeded_at": "2026-04-13T10:05:31Z",
      "lag_minutes": 12
    }
  ],
  "generated_at": "2026-04-13T10:17:00Z"
}
```

Example trend response:

```json
{
  "series": [
    {"key": "flaky_rate_pct", "type": "line", "points": [["2026-04-07", 4.2]]},
    {"key": "total_failure_like_count", "type": "bar", "points": [["2026-04-07", 120]]}
  ],
  "meta": {
    "repo": "pingcap/tidb",
    "branch": "master",
    "granularity": "day"
  }
}
```

Example share response:

```json
{
  "categories": ["FLAKY_TEST", "UNCLASSIFIED"],
  "groups": [
    {"name": "IDC", "values": [22, 78]},
    {"name": "GCP", "values": [30, 70]}
  ]
}
```

## 8.4 Failure Category API Note

Because `failure_category` is intentionally conservative in V1:

- API responses should expose `UNCLASSIFIED` when `failure_category IS NULL`
- chart labels and tooltips should avoid implying root-cause certainty
- optional future enhancement:
  - include `has_flaky_case_match_count` in chart metadata for stricter evidence visibility

## 8.5 Endpoint Contract Summary

The route list above is intentionally stable enough for frontend scaffolding. The following table defines the minimum contract each endpoint should honor in V1.

| Endpoint | Required Query Params | Response Shape | Notes |
| --- | --- | --- | --- |
| `GET /healthz` | none | `{"status":"ok"}` | Readiness probe |
| `GET /livez` | none | `{"status":"ok"}` | Liveness probe |
| `GET /readyz` | none | `{"status":"ok"}` or non-200 on startup failure | Optional separate readiness gate |
| `GET /api/v1/status/freshness` | none | `{"jobs":[...],"generated_at":"..."}` | Reads from `ci_job_state` |
| `GET /api/v1/filters/repos` | optional `start_date`, `end_date` | `{"items":[{"value":"...","label":"..."}]}` | Sorted distinct repo list |
| `GET /api/v1/filters/branches` | optional `repo` | `{"items":[...]}` | Prefer `target_branch`, fallback to `base_ref` |
| `GET /api/v1/filters/jobs` | optional `repo`, `branch` | `{"items":[...]}` | Distinct `job_name` values |
| `GET /api/v1/filters/cloud-phases` | none | `{"items":[...]}` | Usually `GCP`, `IDC` |
| `GET /api/v1/flaky/trend` | optional shared filters | trend response | Main flaky rate over time |
| `GET /api/v1/flaky/composition` | optional shared filters | trend response | Breakdown of `is_flaky` vs `is_retry_loop` signals |
| `GET /api/v1/flaky/top-jobs` | optional shared filters, optional `limit` | ranked response | Top contributing jobs by flaky build count |
| `GET /api/v1/flaky/period-comparison` | `period_a_start`, `period_a_end`, `period_b_start`, `period_b_end`, optional shared filters except `start_date`/`end_date` | comparison response | Used for before/after comparison |
| `GET /api/v1/builds/outcome-trend` | optional shared filters | trend response | Build outcome distribution over time |
| `GET /api/v1/builds/duration-trend` | optional shared filters | trend response | Queue, run, total durations |
| `GET /api/v1/builds/cloud-comparison` | optional shared filters | comparison response | Compare timing/outcome by `cloud_phase` |
| `GET /api/v1/failures/category-trend` | optional shared filters | trend response | `FLAKY_TEST` plus `UNCLASSIFIED` bucket |
| `GET /api/v1/failures/category-share` | optional shared filters | share response | Current category share by filter scope |

Supporting response conventions:

- `trend response`: `{"series":[...],"meta":{...}}`
- `ranked response`: `{"items":[{"name":"...","value":123}],"meta":{...}}`
- `comparison response`: `{"groups":[...],"meta":{...}}`
- `share response`: `{"categories":[...],"groups":[...],"meta":{...}}`

Validation rules:

- reject invalid date ranges with `400`
- reject unsupported `granularity` with `400`
- cap `limit` on ranking endpoints to a safe maximum such as `50`
- return empty arrays, not `null`, for empty filter scopes

## 8.6 Query Ownership by Route Module

Recommended route-to-query ownership:

| Route Module | Query Module Responsibility |
| --- | --- |
| `routes/filters.py` | simple distinct filter queries and option sorting |
| `routes/flaky.py` | flaky trend, composition, top-jobs, period comparison |
| `routes/builds.py` | build outcome, duration, cloud comparison |
| `routes/failures.py` | failure category trend and share |
| `routes/status.py` | freshness/status queries from `ci_job_state` |

## 9. Frontend Implementation

Frontend framework:

- React

Recommended data layer:

- one small API client module under `web/src/api`
- one query hook per chart or per page section

Recommended sections:

1. Overview
2. Flaky Test
3. Failure Classification

Recommended global filters:

- repo selector
- branch selector
- job selector
- date range
- cloud phase selector

Frontend implementation note:

- failure-category visuals should label the null bucket as `UNCLASSIFIED`
- UI should not imply that `UNCLASSIFIED` means `CODE_DEFECT`

## 9.1 Page-to-Endpoint Mapping

| Page | Required Endpoints | Notes |
| --- | --- | --- |
| Overview | `/api/v1/builds/outcome-trend`, `/api/v1/builds/duration-trend`, `/api/v1/builds/cloud-comparison` | Default landing page |
| Flaky Test | `/api/v1/flaky/trend`, `/api/v1/flaky/composition`, `/api/v1/flaky/top-jobs`, `/api/v1/flaky/period-comparison` | Main analytical page for V1 |
| Failure Classification | `/api/v1/failures/category-trend`, `/api/v1/failures/category-share` | Must present `UNCLASSIFIED` carefully |

Recommended shared components for first scaffold:

| Component | Responsibility |
| --- | --- |
| `AppShell` | Layout, navigation, and global filter state |
| `GlobalFilters` | Repo, branch, job, date range, cloud phase controls |
| `ChartCard` | Shared chart chrome with loading and empty states |
| `TrendChart` | Reusable line/bar time-series wrapper |
| `RankedBarChart` | Top job ranking visualization |
| `ComparisonChart` | Period or cloud comparison visualization |
| `ShareChart` | Failure category share visualization |
| `FreshnessBadge` | Render `/api/v1/status/freshness` summary in the header |

## 10. Dashboard App Packaging

Use a multi-stage image:

1. Node stage
   - install web dependencies
   - build React assets

2. Python stage
   - install Python package
   - copy built frontend assets into FastAPI static directory
   - run FastAPI with Uvicorn

Recommended runtime behavior:

- mount no frontend dev server in production
- serve static assets directly from FastAPI in V1

## 11. Kubernetes Deployment

## 11.1 Proposed Charts

```text
charts/
  ci-dashboard/
  ci-dashboard-jobs/
```

`charts/ci-dashboard`:

- Deployment
- Service
- Ingress or HTTPRoute
- Config/Secret references
- probe paths matching existing repo chart conventions:
  - `/healthz`
  - `/livez`

`charts/ci-dashboard-jobs`:

- CronJob: `ci-sync-builds`
- CronJob: `ci-sync-pr-events`
- CronJob: `ci-refresh-build-derived`

Why split charts:

- dashboard app and jobs have different rollout cadence
- failed job rollout should not require dashboard Deployment churn
- app chart can reuse the existing `Deployment + Service + Ingress/HTTPRoute` pattern already used by charts such as `publisher` and `tibuild-v2`

## 11.2 Dashboard App Deployment Values

Recommended values:

- image repository/tag
- replica count
- ingress host/path
- app environment variables
- TiDB secret reference

Recommended app env:

- `TIDB_HOST`
- `TIDB_PORT`
- `TIDB_USER`
- `TIDB_PASSWORD`
- `TIDB_DB`
- `TIDB_SSL_CA`

## 11.3 CronJob Schedules

Initial V1 schedules:

- `ci-sync-builds`: `5 * * * *`
- `ci-sync-pr-events`: `15 * * * *`
- `ci-refresh-build-derived`: `25 * * * *`

Recommended CronJob settings:

- `concurrencyPolicy: Forbid`
- `successfulJobsHistoryLimit: 2`
- `failedJobsHistoryLimit: 5`
- `restartPolicy: Never`

## 12. Test Plan

## 12.1 Job Tests

Minimum job tests:

- exact retest parser
- build URL normalizer
- source JSON extraction from `prow_jobs.spec` and `prow_jobs.status`
- `ci-sync-builds` upsert behavior
- `ci-sync-pr-events` candidate PR selection
- flaky flag recomputation on known pilot scenarios
- `has_flaky_case_match` windowed URL matching

## 12.2 API Tests

Minimum API tests:

- filter endpoints return stable shapes
- chart endpoints validate parameters
- failure-category endpoints map null to `UNCLASSIFIED`

## 12.3 Manual Validation

Manual checks after first backfill:

1. compare `ci_l1_builds` row growth against `prow_jobs`
2. sample `target_branch` backfill on known PR builds
3. sample `cloud_phase` URL classification on both GCP and IDC URLs
4. sample exact retest parsing to ensure bot instruction comments are excluded
5. sample `has_flaky_case_match` on known `pingcap/tidb` builds

## 13. First Implementation Order

Recommended order:

1. create SQL migrations for:
   - `ci_l1_builds`
   - `ci_l1_pr_events`
   - `ci_job_state`
2. scaffold Python package with shared `common` module
3. implement `ci-sync-builds`
4. implement `ci-sync-pr-events`
5. implement `ci-refresh-build-derived`
6. run initial backfill and validate row counts
7. scaffold FastAPI app and health endpoints
8. implement filter endpoints
9. implement Flaky Test chart endpoints
10. scaffold React app and global filter shell
11. implement Flaky Test page
12. package app image and jobs image
13. add Helm charts
14. deploy to prow Kubernetes

## 14. Deferred Implementation Items

Deferred after V1:

- pod event collector
- Jenkins log collector
- physical aggregate tables
- direct ingestion of full GitHub review/label workflow events
- dedicated `ci_l1_test_case_runs`
