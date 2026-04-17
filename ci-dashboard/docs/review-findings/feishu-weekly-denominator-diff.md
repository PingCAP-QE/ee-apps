# DATA-004 Findings: Feishu Weekly-Value Denominator Difference

## Scope

Goal: explain weekly-value differences for the same issue between dashboard and Feishu reference table, then quantify what is still unexplained.

Compared datasets:
- Feishu-side reference artifact:  
  `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issue_desc_branch_master_table_20260410_135636.md`
- Dashboard API/TiDB logic (`issue-weekly-rates`) from current backend code.

Comparison window for Feishu-aligned columns:
- `2026-03-02`, `2026-03-09`, `2026-03-16`, `2026-03-23`, `2026-03-30`, `2026-04-06`.

## Confirmed Findings

1. Denominator differences are systematic, not random.
- Feishu table has `16` cases x `6` weeks = `96` cells.
- Using current API coverage for those same 16 cases (closed rows from `issue_status=closed`, and the 4 reopened rows from `issue_status=open`):
  - denominator exact matches: `17/96`
  - denominator mismatches: `79/96`

2. All 79 denominator mismatches fit exactly into 3 deterministic delta vectors (API minus Feishu).
- Vector A (`14` cases): `[+2, +8, +1, +22, +0, +649]`
- Vector B (`1` case, `TestLimitPushdown`): `[+1, +4, +0, +11, +0, +285]`
- Vector C (`1` case, `TestServerInfo`): `[+1, +4, +1, +11, +0, +364]`
- No outlier denominator pattern was found.

3. Those 3 vectors map directly to case job-scope composition.
- Current case-job-scope query result:
  - `14` cases map to both jobs: `ghpr_unit_test` + `pull_unit_test_next_gen` -> Vector A
  - `TestLimitPushdown` maps only `ghpr_unit_test` -> Vector B
  - `TestServerInfo` maps only `pull_unit_test_next_gen` -> Vector C
- This is consistent with denominator formula in backend (`COUNT(DISTINCT normalized_build_key)` per case-job-scope).

4. The large `+649` at week `2026-04-06` is fully attributable to job-level build-count gap (`ci_l1` vs `tmp`).
- For `TestAuditPluginRetrying` scope jobs on week `2026-04-06`:
  - `ghpr_unit_test`: `ci_l1=489`, `tmp=204` -> `+285`
  - `pull_unit_test_next_gen`: `ci_l1=574`, `tmp=210` -> `+364`
  - total `+649`
- This same two-job structure explains why the same `+649` appears across most cases.

5. The denominator delta decomposition is complete.
- For two-job cases, vector A can be decomposed as:
  - snapshot drift (`tmp_now - Feishu_snapshot`): `[+2, +1, +1, +0, +0, +0]`
  - data-coverage delta (`ci_l1 - tmp_now`): `[+0, +7, +0, +22, +0, +649]`
  - sum: `[+2, +8, +1, +22, +0, +649]`
- Equivalent decomposition also matches Vector B and Vector C.

6. Weekly numerators differ less and mostly in the latest week, consistent with freshness effects.
- Numerator exact matches: `85/96`.
- Numerator mismatches: `11/96` (all positive deltas, no negative deltas).
- Mismatch concentration:
  - week `2026-04-06`: `9` cells, total `+56`
  - week `2026-03-23`: `1` cell, total `+1`
  - week `2026-03-09`: `1` cell, total `+1`

## Remaining Gaps (Quantified)

1. Denominator explanation gap:
- `0/96` cells unexplained after applying the observed 3-vector + job-scope decomposition.

2. Numerator explanation gap:
- Mechanism is strongly indicated as data freshness/backfill drift, but exact Feishu share-anchor extraction was not used here as a second independent proof.
- Residual uncertainty is low and limited to proving exact as-of cutoff timing, not to query logic shape.

## Closure Readiness

DATA-004 can be moved closer to closure.

Suggested status movement:
- from `INVESTIGATING` to `READY_TO_CLOSE` (or equivalent) with one pending product decision:
  - whether dashboard acceptance should follow current `ci_l1` system-of-record behavior, or intentionally preserve legacy `tmp` snapshot continuity for visual parity.

If that policy decision is made, this item is effectively closure-ready from a technical root-cause perspective.

## Evidence

- Backend denominator logic:
  - `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/api/queries/flaky.py` (`case_job_scope`, `case_weekly_denominator`).
- Feishu reference artifact (master table):
  - `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issue_desc_branch_master_table_20260410_135636.md`
- Snapshot/legacy reference CSV:
  - `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_case_weekly_rates_master_targetbranch_with0302_20260409_163208.csv`
- Live API checks:
  - `http://127.0.0.1:8000/api/v1/flaky/issue-weekly-rates?repo=pingcap/tidb&branch=master&start_date=2026-03-02&end_date=2026-04-13&issue_status=closed`
  - `http://127.0.0.1:8000/api/v1/flaky/issue-weekly-rates?repo=pingcap/tidb&branch=master&start_date=2026-03-02&end_date=2026-04-13&issue_status=open`
- TiDB validation:
  - job-level denominator breakdown in `ci_l1_builds` and `tmp_builds` for `TestAuditPluginRetrying`, including week `2026-04-06`.
