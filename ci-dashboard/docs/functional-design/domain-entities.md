# Domain Entities

## E1: `ci_l1_builds`

Purpose:

- normalized build fact table
- one row per `prow_jobs.prowJobId`

Key fields:

| Field | Meaning |
| --- | --- |
| `source_prow_row_id` | upstream `prow_jobs.id` watermark source |
| `source_prow_job_id` | unique logical build id from `prow_jobs.prowJobId` |
| `repo_full_name` | normalized `org/repo` |
| `pr_number` | PR number when this is a PR build |
| `is_pr_build` | boolean derived from `pr_number` |
| `normalized_build_key` | normalized build URL used for case matching |
| `pending_time` | normalized queue-entry time from `status` JSON |
| `start_time` | build start time |
| `completion_time` | build completion time |
| `queue_wait_seconds` | `pending_time - start_time` |
| `run_seconds` | `completion_time - pending_time` |
| `total_seconds` | `completion_time - start_time` |
| `head_sha` | head SHA from `spec.refs.pulls[0].sha` |
| `target_branch` | best-effort PR base branch backfilled later; may remain null |
| `cloud_phase` | `GCP` or `IDC` classification |
| `is_flaky` | build-level flaky flag |
| `is_retry_loop` | build-level retry-loop flag |
| `has_flaky_case_match` | stricter case-level evidence flag |
| `failure_category` | V1 failure category, conservative |

Current implementation status:

- schema is defined in [001_create_ci_l1_builds.sql](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/sql/001_create_ci_l1_builds.sql)
- first ingest path is implemented in [sync_builds.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_builds.py)

## E2: `ci_l1_pr_events`

Purpose:

- normalized PR event and PR snapshot table
- one row per imported timeline event or synthetic `pr_snapshot` row when a matching `github_tickets` record exists

Key fields:

| Field | Meaning |
| --- | --- |
| `repo` | normalized repo string such as `pingcap/tidb` |
| `pr_number` | PR number |
| `event_key` | idempotent logical event id |
| `event_time` | normalized event timestamp |
| `event_type` | `pr_snapshot`, `committed`, or `retest_comment` |
| `comment_id` | comment id when event is a comment |
| `comment_body` | raw comment body for auditability |
| `retest_event` | exact retest command boolean |
| `commit_sha` | commit SHA for commit events |
| `target_branch` | best-effort `branches.base.ref` |
| `head_ref` | `branches.head.ref` |
| `head_sha` | `branches.head.sha` |

Event key rules:

- `pr_snapshot`
- `retest_comment:{comment_id}` or deterministic fallback
- `committed:{commit_sha}:{event_time}` or deterministic fallback

Current implementation status:

- schema is defined in [002_create_ci_l1_pr_events.sql](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/sql/002_create_ci_l1_pr_events.sql)
- sync job is implemented in [sync_pr_events.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/sync_pr_events.py)

## E3: `ci_job_state`

Purpose:

- persist watermarks and last-run metadata for each job

Key fields:

| Field | Meaning |
| --- | --- |
| `job_name` | logical job id |
| `watermark_json` | current incremental progress payload |
| `last_started_at` | last start time |
| `last_succeeded_at` | last success time |
| `last_status` | `never`, `running`, `succeeded`, or `failed` |
| `last_error` | most recent error text |

Watermark shapes:

- `ci-sync-builds`: `{"last_source_prow_row_id": N}`
- `ci-sync-pr-events`: `{"last_ticket_updated_at": "...", "last_build_source_prow_row_id_seen": N}`
- `ci-refresh-build-derived`: `{"last_processed_build_id": N, "last_processed_pr_event_updated_at": "...", "last_processed_case_report_time": "..."}`

Current implementation status:

- schema is defined in [003_create_ci_job_state.sql](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/sql/003_create_ci_job_state.sql)
- helper logic is implemented in [state_store.py](/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/jobs/state_store.py)
