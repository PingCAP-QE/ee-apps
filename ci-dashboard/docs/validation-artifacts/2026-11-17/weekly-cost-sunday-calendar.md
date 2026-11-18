# Sunday–Saturday UTC Weekly Cost validation

## Scope and automated checks

CI and QA Weekly now use the previous complete Sunday–Saturday UTC week. One
shared week-start helper changes only these reports and their weekly cumulative
charts. No schema, ingestion, accounting, API-shape, daily/plan/month-scope, or
generic Cost/CI weekly-bucketing change is required.

- Backend: **1,808 passed**, full-project statement coverage **91.97%** (>90%).
- Frontend: **89 passed**, none skipped; production build passed.
- Ruff and `git diff --check`: passed.
- Added 11 parameterized integration cases: Saturday/Sunday rollover,
  Monday/Tuesday reviews, month/year boundaries, Sunday/Saturday billing facts,
  next-Sunday exclusion, cumulative weekly buckets, cross-panel reconciliation,
  and unchanged previous/current-natural-month behavior.
- Independent **pi-DeepSeek review-loop: CLEAN**. Optional review hardening adds
  cross-section calendar assertions at every parameterized boundary; no production
  logic fix was needed.

Commands (run from `ci-dashboard/`, except the `cd web` step):

```bash
LC_COLLATE=C COVERAGE_FILE=/tmp/weekly-cost.coverage make test-cov PYTHON=./.venv/bin/python
make lint PYTHON=./.venv/bin/python
(cd web && npm test && npm run build)
git diff --check
```

`LC_COLLATE=C` avoids the macOS Bash 3.2 locale-dependent control-character
range behavior in existing shell-renderer tests. No tests were skipped and no
renderer code was changed. `LC_ALL=C` is not equivalent: it breaks the existing
Unicode character-length checks.

## Production-data reconciliation

Executed after the first clean DeepSeek change review, on **2026-11-17 UTC**.
The new local query code and independent SQL read the production TiDB database
in the **same transaction snapshot**, with a SQLAlchemy statement guard rejecting
anything except `SELECT`, `SHOW`, or `EXPLAIN`. No writes, migrations, backfills,
workload changes, or production deployment were performed.

**404 assertions passed**, using **786 source/day aggregates** across 13 QA
accounts and the two fixed CI accounts. Windows:

- Report: **2026-11-08–2026-11-14** (Sunday–Saturday, inclusive).
- WoW baseline: **2026-11-01–2026-11-07**.
- Eight-week history: **2026-09-20–2026-11-14**.
- Previous natural month: **2026-10-01–2026-10-31**, unchanged.

| Value | New query result, USD | Independent SQL |
| --- | ---: | --- |
| CI GCP weekly list cost | 2,413.64 | Matched |
| CI Tencent weekly net cost | 3,224.88 | Matched |
| QA weekly list cost | 12,983.04 | Matched |
| QA previous-month list cost | 81,722.95 | Matched |

Checks include every account and all eight cost-history buckets; last-bucket
reconciliation against account/summary/category totals; full versus progressively
loaded CI sections and QA trend/allocation endpoints; team/repository share
totals; unchanged natural-month totals; and all eight CI weekly budget points
independently prorated from raw budget plans. Monetary comparisons allow at most
USD 0.01 of existing display rounding.

Calendar arithmetic and the following accounting expressions were independently
written for the SQL reference, without calling the changed week-start helper or
production cost-expression helpers:

```sql
SELECT c.vendor, c.account_id, c.usage_date, COUNT(*) AS row_count,
  SUM(COALESCE(CASE
    WHEN c.vendor = 'gcp'
      AND c.sku_name LIKE 'Compute Flexible Committed Use Discounts%' THEN 0
    WHEN c.currency = 'CNY' THEN c.list_cost / 6.5
    ELSE c.list_cost END, 0)) AS list_usd,
  SUM(COALESCE(CASE WHEN c.currency = 'CNY' THEN c.net_cost / 6.5
    ELSE c.net_cost END, 0)) AS net_usd
FROM cost_attribution_daily c
LEFT JOIN cost_sources s ON s.vendor = c.vendor AND s.account_id = c.account_id
WHERE c.usage_date BETWEEN :history_start AND :following_sunday
  AND ((c.vendor = 'gcp' AND c.account_id = 'pingcap-testing-account')
    OR (c.vendor = 'tencent' AND c.account_id = '100050658403')
    OR (s.is_active = 1 AND s.category = 'QA'))
GROUP BY c.vendor, c.account_id, c.usage_date;
```

For each CI/QA account, the shifted-window conservation check also passed:
`new Sun–Sat cost = old Mon–Sun cost + first Sunday cost - following Sunday cost`.
This verifies the intended date replacement rather than expecting differently
scoped weekly totals to remain equal.

## Data-availability limitations (not calculation failures)

Both CI accounts have rows on all seven report dates. Eleven of thirteen QA
accounts also have seven observed dates. The QA Alibaba source has rows only
through November 12 (November 13–14 absent), and one configured Azure source has
no rows in the report week. These are source-availability observations, not proof
of zero spend or a newly introduced aggregation mismatch.

The calendar shift adds one day of arrival buffer; it does **not** establish
billing finality or repair missing upstream exports. Both pages retain a delayed-
billing notice and CI retains its missing-date warnings. Later imports or billing
corrections may revise these totals. This evidence validates the proposed code
against the production data snapshot, not a deployed rollout.
