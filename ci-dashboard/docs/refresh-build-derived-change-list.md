# `ci-refresh-build-derived` Change List

Last updated: 2026-04-21

## Scope

This document lists the landed code changes for the `ci-refresh-build-derived` job, plus the operational behavior we used during the recent backlog catch-up.

Primary landed commit:

- `da86f3bbfdef348a68f0fca9cdd86f4c33e45ccb`
- subject: `fix(ci-dashboard): improve refresh-build-derived cronjob performance (#415)`

## Landed Code Changes

### 1. Add per-run impacted build limit

The job no longer tries to process the entire backlog in one run.

- New config knob: `CI_DASHBOARD_REFRESH_BUILD_LIMIT`
- Default value: `5000`
- Purpose:
  - cap one run to a bounded number of impacted builds
  - shorten each cron execution
  - make catch-up resumable instead of timing out on a huge backlog

Implementation details:

- `run_refresh_build_derived()` now resolves `refresh_build_limit`
- `_get_impacted_build_ids()` accepts `max_builds`
- the selection result is truncated to at most `max_builds`
- the job records whether there is more work to continue later

### 2. Freeze the selection window in `ci_job_state`

The job now distinguishes between:

- the current progress watermark
- the frozen target watermark for the current catch-up slice

New watermark fields:

- `pending_refresh`
- `pending_target_build_id`
- `pending_target_pr_event_updated_at`
- `pending_target_case_report_time`

Behavior:

- when a run cannot finish the full selected window, it writes:
  - the last processed build id for this slice
  - the frozen target upper bound of the slice
  - `pending_refresh = true`
- the next run resumes against the same frozen window instead of re-scanning a moving head
- once the frozen slice is fully consumed, `pending_refresh` is cleared

This is the key change that makes repeated catch-up runs deterministic and idempotent.

### 3. Preserve incremental semantics for PR-event and case-driven refresh

The impacted-build selection now combines three sources within the frozen window:

- newly inserted `ci_l1_builds`
- `ci_l1_pr_events` updated after the last processed event watermark
- `problem_case_runs` reported after the last processed case watermark

When `pending_refresh = true`, the PR-event and case-driven scans are also constrained by:

- the frozen target watermark
- the already processed lower build id

That prevents each retry from reopening the whole historical universe.

### 4. Rework flaky flag recomputation into group chunks

The flaky / retry-loop recomputation path now works in bounded group chunks.

- impacted groups are collected first
- groups are processed in `refresh_group_batch_size` chunks
- each chunk opens its own transaction
- summary counters are accumulated chunk by chunk

This reduces transaction size and avoids one giant all-or-nothing recomputation.

Related knob:

- `CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE`
- default value remains `25`

### 5. Batch `failure_category` refresh in smaller build chunks

The final failure-category phase now runs in chunked build batches rather than one oversized write.

Behavior:

- branch enrichment runs in build chunks
- flaky flag recomputation runs in group chunks
- case evidence runs in build chunks
- `failure_category` refresh runs only for:
  - the directly impacted builds
  - the builds whose flaky flags changed during recomputation

This keeps the write set bounded and makes late phases much cheaper.

### 6. Strengthen watermark validation

The job now validates persisted watermark fields instead of silently accepting malformed state.

Behavior:

- corrupted values such as non-integer `last_processed_build_id`
- now raise `ValueError("Invalid ci-refresh-build-derived watermark ...")`

This prevents bad state from causing silent mis-processing.

### 7. Expose the new behavior in config and docs

The configuration layer now parses and validates:

- `CI_DASHBOARD_REFRESH_BUILD_LIMIT`

The README and domain-entity docs were updated to explain:

- the new env knob
- the extended watermark shape

## Files Changed

Files changed by the landed `#415` code change:

- [README.md](/Users/dillon/workspace/ee-apps/ci-dashboard/README.md)
- [domain-entities.md](/Users/dillon/workspace/ee-apps/ci-dashboard/docs/functional-design/domain-entities.md)
- [config.py](/Users/dillon/workspace/ee-apps/ci-dashboard/src/ci_dashboard/common/config.py)
- [refresh_build_derived.py](/Users/dillon/workspace/ee-apps/ci-dashboard/src/ci_dashboard/jobs/refresh_build_derived.py)
- [test_refresh_build_derived.py](/Users/dillon/workspace/ee-apps/ci-dashboard/tests/jobs/test_refresh_build_derived.py)
- [test_config_and_db.py](/Users/dillon/workspace/ee-apps/ci-dashboard/tests/test_config_and_db.py)

## Test Coverage Added By This Change

The landed tests cover these cases:

- slicing a large backlog across multiple runs
- freezing the selection window while more builds arrive later
- clearing `pending_refresh` when the remaining slice is empty
- rejecting corrupted watermark payloads
- picking up new `problem_case_runs` incrementally
- validating `CI_DASHBOARD_REFRESH_BUILD_LIMIT`

## Operational Notes From Recent Catch-up

These are runtime operations we used during backlog recovery. They are not additional code changes in `ee-apps`, but they are part of how this job is currently being operated.

### Manual catch-up mode

For historical backlog catch-up, we temporarily used one-off Kubernetes Jobs cloned from the CronJob template, with:

- `CI_DASHBOARD_REFRESH_BUILD_LIMIT=10000`

Reason:

- the default cron limit of `5000` was safe but too slow for a large historical backlog

### Cron behavior during catch-up

During manual catch-up we kept the CronJob suspended, because running both of these at once is undesirable:

- scheduled `5000` cron executions
- one-off `10000` catch-up jobs

Operational rule:

- during backlog catch-up: `suspend=true`
- when backlog is close enough: resume the CronJob

### Current code vs current ops

Current code default:

- `CI_DASHBOARD_REFRESH_BUILD_LIMIT=5000`

Current manual catch-up practice:

- one-off jobs may override the limit to `10000`
- this override is operational and is not baked into the application default

## Short Summary

If we compress all job-related changes into one sentence:

- the job was changed from a potentially huge moving-window refresh into a bounded, resumable, checkpointed refresh pipeline that can safely chew through backlog over many runs.
