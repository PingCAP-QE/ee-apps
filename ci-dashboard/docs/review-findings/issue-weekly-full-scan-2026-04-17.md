# Filtered-Issue Weekly Table Full Scan

Date: 2026-04-17

## Scope

- endpoint: `GET /api/v1/flaky/issue-weekly-rates`
- repo: `pingcap/tidb`
- branch: `master`
- window: `2026-03-02` to `2026-04-17`
- issue statuses checked:
  - `closed`
  - `open`

## Why This Check Exists

This sweep was run after fixing the false-positive bug in the `Filtered-issue weekly case table`.

The previous bug came from joining `problem_case_runs` to `ci_l1_builds` too loosely by normalized build key, which allowed:

- IDC and GCP build-key collisions
- stale old case rows to match much newer builds

The purpose of this sweep is to confirm that the fixed logic does not still produce cells where the dashboard shows a non-zero weekly flaky count but the raw `problem_case_runs` table has no same-week flaky evidence for that case.

## Validation Method

For each of the two `issue_status` scopes:

1. fetch the API payload from `issue-weekly-rates`
2. enumerate every returned row and every returned week cell
3. for each cell where API `flaky_runs > 0`, check whether raw `problem_case_runs` contains the same `repo`, `branch`, `case_name`, and week with at least one `flaky = 1` row
4. classify a residual false positive only when:
   - API cell is non-zero
   - same-week raw `problem_case_runs.flaky = 1` count is zero

This is intentionally a raw-evidence check, not a Feishu parity check and not a daily-report ranking comparison.

## Result

### `issue_status=closed`

- returned issue rows: `19`
- non-zero weekly cells scanned: `67`
- residual false-positive cells: `0`
- latest week checked: `2026-04-13`
- latest-week non-zero cases: `4`

Latest-week supported closed cases:

- `#67174` `TestGlobalMemoryTuner`
- `#66373` `TestKillAutoAnalyze`
- `#67626` `TestServiceTracksSuccessfulCheckpoint`
- `#67355` `TestSubsetIdxCardinality`

### `issue_status=open`

- returned issue rows: `42`
- non-zero weekly cells scanned: `216`
- residual false-positive cells: `0`
- latest week checked: `2026-04-13`
- latest-week non-zero cases: `27`

## Conclusion

For the current main review scope on `pingcap/tidb/master`, the false-positive bug in `Filtered-issue weekly case table` is no longer reproducible in this full sweep.

Specifically:

- all `283` non-zero cells scanned in the current window had same-week raw flaky evidence
- latest week `2026-04-13` had `0` residual false positives in both `closed` and `open` issue scopes

This does not mean the dashboard is now byte-for-byte identical to the Feishu artifact or to the daily flaky report, because those comparisons have separate denominator, snapshot, and presentation semantics.
It does mean the previously observed dashboard behavior of showing recent-week flaky counts with no real recent-week case evidence is fixed for this full review scope.
