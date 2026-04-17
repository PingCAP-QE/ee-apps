# DATA-003 Feishu Issue Set Diff

## Scope

- Question: under `repo=pingcap/tidb`, `branch=master`, `issue_status=closed`, why do issue rows differ from the Feishu table.
- Backend baseline: local API endpoint `GET /api/v1/flaky/issue-weekly-rates` with real TiDB data.
- Comparison baseline (Feishu-side artifact in local env):  
  `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issue_desc_branch_master_table_20260410_135636.md`
- Time window used for apples-to-apples row-set check: `start_date=2026-03-02`, `end_date=2026-04-06` (same week columns as that Feishu table artifact).

## Confirmed Findings

1. Row-set mismatch is real and reproducible.
- Local API-equivalent issue set (TiDB, current data) returns `18` rows.
- Feishu artifact table has `16` rows.
- Recheck time: `2026-04-15` (local evidence refreshed).

2. Feishu-only rows (`4`) are excluded by current API because those issues are currently `open`.
- Feishu-only: `TestCancelWhileScan`, `TestFinishStmtError`, `TestIterationOfRunningJob`, `TestResourceGroupRunaway`.
- In TiDB now, all four have `issue_status=open` and non-null `last_reopened_at`:
  - `66982 TestCancelWhileScan` reopened at `2026-04-09 07:16:43`
  - `66728 TestFinishStmtError` reopened at `2026-04-09 07:18:05`
  - `67177 TestIterationOfRunningJob` reopened at `2026-04-09 07:19:04`
  - `66977 TestResourceGroupRunaway` reopened at `2026-04-10 00:05:44`
- Current backend issue scope applies strict current-status filter:
  - `LOWER(fi.issue_status) = :issue_status` when `issue_status=closed`.
  - Code: `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/api/queries/flaky.py` lines `655-657`.

3. API-only rows (`6`) are explained by source-scope differences (not denominator math).
- API-only: `TestAuditLogNormal`, `TestDMLWithLiteCopWorker`, `TestGetAndResetRecentInfoSchemaTS`, `TestKillAutoAnalyze`, `TestNextGenMeteringWithConflictResolution`, `TestSubsetIdxCardinality`.
- `4` of them were created on `2026-02-25` and are included by API because backend has no lower bound on `issue_created_at` tied to `start_date`.
- `TestNextGenMeteringWithConflictResolution` is authored by `yinsustart`; API includes it because backend has no `author=ti-chi-bot` filter.
- `TestSubsetIdxCardinality` closed on `2026-04-12`, later than the Feishu artifact snapshot time (`20260410`), so it appears in API now but not that artifact.

4. Backend date-window semantics for closed issues differ from the Feishu workflow artifacts.
- Backend does:
  - `fi.issue_created_at < end_date + 1 day` (upper bound on creation time).
  - for `start_date`: `(status != closed OR issue_closed_at IS NULL OR issue_closed_at >= start_date)` (lower bound on close time only).
  - no `issue_closed_at <= end_date` condition.
  - Code: `/Users/dillon/workspace/ee-apps-worktrees/ci-dashboard-v1/ci-dashboard/src/ci_dashboard/api/queries/flaky.py` lines `659-667`.
- Feishu playbook artifacts were generated from a closed-issue snapshot workflow with:
  - `state:closed author:ti-chi-bot`,
  - typical created-time cutoff `>= 2026-03-01`.
  - Doc reference: `/Users/dillon/workspace/ci_metrics_sample/docs/flask_case_update_playbook.md` lines `17-20`.

## Close-Down Status for DATA-003

What can be closed now with local evidence:
- We can explain the full `10`-case symmetric diff (`4` Feishu-only + `6` API-only) using current-status filtering, source-snapshot timing, lack of author filter, and lack of created-time lower bound.
- This means DATA-003 is no longer blocked on unknown backend behavior. The mismatch is attributable to semantic/snapshot differences, not random drift.

Exact unresolved gap that remains:
- We still do not have a direct extraction of the exact Feishu share-anchor block (`#share-MtPldql5XoDdU2xj7M9cTle4ned`) as rendered in Feishu at comparison time.
- Therefore, we cannot prove with local-only evidence whether the linked table is exactly identical to local artifact file:
  `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issue_desc_branch_master_table_20260410_135636.md`
- Residual uncertainty is now narrow: it is only about Feishu share-block fidelity / timestamped content, not about dashboard-side set construction logic.

## Evidence

- API result captured locally:
  - TiDB recheck on `2026-04-15` confirms API-equivalent set size `18` under:
    `repo=pingcap/tidb`, `branch=master`, `issue_status=closed`,
    `start_date=2026-03-02`, `end_date=2026-04-06`.
- Feishu-side local artifacts:
  - `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issue_desc_branch_master_table_20260410_135636.md` (`16` rows).
  - `/Users/dillon/workspace/ci_metrics_sample/reports/adhoc_tidb_master/flaky_issues_closed_tichi_from_web_since_202603_20260409_120054.csv` (contains the 4 Feishu-only rows and excludes the 6 API-only rows above).
- TiDB verification for the 10 diff cases confirmed:
  - current status/open+reopen timestamps for Feishu-only 4,
  - created-at `2026-02-25` for 4 API-only rows,
  - non-`ti-chi-bot` author (`yinsustart`) for one API-only row,
  - late close (`2026-04-12`) for `TestSubsetIdxCardinality`.
- Feishu direct-access attempts:
  - direct `curl` on share URL: no table content (fragment not directly retrievable server-side).
  - OpenAPI auth/get-node/raw-content succeeded, but raw content and fetched block text did not expose this case table row text (no hits on target case names), so exact share-anchor table extraction remains incomplete.

## Next Checks

1. Decide target semantics for `issue_status=closed` in dashboard:
- Option A: current status closed (current behavior).
- Option B: closed during selected window (time-aware status snapshot).
- Option C: mirror legacy Feishu workflow (`author=ti-chi-bot`, created cutoff, snapshot timing behavior).

2. If we want Feishu parity as a mode, add an explicit compatibility filter set:
- `author=ti-chi-bot`,
- optional created cutoff (for example `>= 2026-03-01` or configurable),
- optional "status-as-of timestamp" instead of current status.

3. For final closure without ambiguity, fetch exact block content for share anchor `MtPldql5XoDdU2xj7M9cTle4ned` via block-id resolution (instead of relying on raw-content text flattening).
