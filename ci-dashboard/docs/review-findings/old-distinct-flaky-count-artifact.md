# Old Distinct Flaky Count Artifact Investigation

Date: 2026-04-17

## Question

Why did the older artifact show:

- `master = 196, 133, 102, 140, 143`

while the current dashboard for the equivalent `pingcap/tidb + master` scope now shows:

- `196, 133, 102, 102, 92`

## Artifact Located

The older values are stored in:

- `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/distinct_flaky_case_count_master_release85_weekly_from_20260309.csv`
- `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/distinct_flaky_case_count_master_release85_weekly_from_20260309.md`

The file content is:

- `master = 196, 133, 102, 140, 143`
- `release-8.5 = 100, 94, 37, 23, 0`

## What Was Verified

### 1. Current dashboard output

Using current dashboard query logic against current TiDB data:

- `2026-03-09 = 196`
- `2026-03-16 = 133`
- `2026-03-23 = 102`
- `2026-03-30 = 102`
- `2026-04-06 = 92`

For the wider range including the next week:

- `2026-04-13 = 68`

### 2. `issue_status=closed` is not part of this table's SQL

The `Distinct flaky case number` table ignores `issue_status`.
It uses repo / branch / job / cloud / time scope only.

### 3. Recent build-key matching fix is not the direct cause

Replaying the current query with and without the newer cloud/time matching constraints on the current source snapshot did not explain the final `92`.

That fix is still real and important for the issue-scoped weekly table, but it does not by itself explain the current distinct-count delta.

### 4. The old artifact has no checked-in generator script

Searches in `ci_metrics_sample/src`, `docs`, and `tests` did not find a checked-in script that generates `distinct_flaky_case_count_master_release85_weekly_from_20260309.csv`.

So this file was likely created by one of:

- a manual ad hoc SQL run
- a manual export from an older local web/API result
- a transient notebook / command that was not committed

### 5. The older `ci_metrics_sample` workflow depended on `tmp_*` tables that are now gone

The sample workflow documentation clearly uses:

- `tmp_builds`
- `tmp_pr_events`
- `problem_case_runs`

But in the current `insight` database:

- `problem_case_runs` still exists
- `tmp_builds` does not exist
- `tmp_pr_events` does not exist

This means the exact old build-scope snapshot used by the sample workflow is no longer reproducible from the current database.

## Strongest Technical Clue

The old suspicious value:

- `2026-03-30 = 140`

can be reproduced from a loose join pattern that:

- joins by normalized build key
- keeps branch equality
- but omits both:
  - cloud disambiguation
  - report-time-to-build-time proximity matching

Under the current source snapshot, that loose join reproduces:

- `2026-03-09 = 196`
- `2026-03-16 = 133`
- `2026-03-23 = 102`
- `2026-03-30 = 140`

which is a very strong match to the old artifact's first four master values except the final week.

## What Cannot Be Reproduced Now

The old final value:

- `2026-04-06 = 143`

cannot be reproduced from the current source snapshot under any consistent current-formula variant that was tested.

It does not match:

- current correct distinct flaky count
- raw `problem_case_runs` distinct flaky cases
- raw any-case counts
- current bug-simulation variants with present-day data

## Conclusion

The old `140 / 143` numbers did not come from the current dashboard logic operating on the current database state.

The most likely explanation is a combination of:

1. an older ad hoc build-scope snapshot from the now-removed `tmp_builds` / `tmp_pr_events` workflow, and
2. a looser historical build-key mapping that was vulnerable to recent-week inflation.

The `2026-03-30 = 140` value has a clear bug-like signature and should be treated as unreliable.
The `2026-04-06 = 143` value cannot be reproduced now and should be treated as an old artifact value from a lost snapshot or manual post-processing step, not as a trustworthy baseline.
