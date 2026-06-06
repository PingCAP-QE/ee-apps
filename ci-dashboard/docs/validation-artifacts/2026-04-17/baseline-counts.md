# Baseline Counts

Date: 2026-04-17

## K8s Job State

| Job | Status | Notes |
| --- | --- | --- |
| `ci-dashboard-eq-prd-backfill-20260416` | `Complete` | terminal log ends with `backfill-range finished` at `2026-04-17 01:46:13` |
| `ci-dashboard-eq-prd-sync-flaky-issues-20260416` | `Complete` | one-off sync job finished successfully |

## Row Counts

| Table | Row Count |
| --- | ---: |
| `prow_jobs` | 654655 |
| `github_tickets` | 178180 |
| `problem_case_runs` | 190601 |
| `ci_l1_builds` | 251375 |
| `ci_l1_pr_events` | 43833 |
| `ci_l1_flaky_issues` | 76 |
| `ci_job_state` | 1 |

## Time Windows

| Table | Min Time | Max Time |
| --- | --- | --- |
| `prow_jobs` | `2025-02-27 19:00:42` | `2026-04-17 02:00:25` |
| `github_tickets` | `2015-10-29 10:14:49` | `2026-04-15 21:34:04` |
| `problem_case_runs` | `2023-10-31 22:55:02` | `2026-04-17 10:13:52` |
| `ci_l1_builds` | `2025-12-01 00:02:20` | `2026-04-16 14:47:26` |
| `ci_l1_pr_events` | `2019-09-25 08:30:33` | `2026-04-15 21:34:04` |
| `ci_l1_flaky_issues` | `2026-04-16 15:44:16` | `2026-04-16 15:44:16` |

## `ci_job_state`

| Job Name | Last Status | Last Started At | Last Succeeded At | Updated At |
| --- | --- | --- | --- | --- |
| `ci-sync-flaky-issues` | `succeeded` | `2026-04-16 15:42:57` | `2026-04-16 15:44:16` | `2026-04-16 15:44:16` |

## Notes

- `backfill-range` is stateless and does not write `ci_job_state`, so one-off backfill completion must be validated with K8s job state and terminal logs rather than `ci_job_state`.
- The new instance currently has only one recurring job state row populated, for `ci-sync-flaky-issues`.
