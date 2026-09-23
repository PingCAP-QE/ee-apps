-- Apply while budget writers are paused (or otherwise serialized). This migration
-- requires the CICD plan scope columns added by sql/028_add_cost_budget_plan_scope.sql.
ALTER TABLE cost_budgets
  ADD COLUMN IF NOT EXISTS cost_basis VARCHAR(16) NOT NULL DEFAULT 'list_cost'
  AFTER budget_amount;

-- This sentinel is executed by `mysql < sql/029_add_ci_budget_cost_basis.sql`.
-- It emits CI_BUDGET_TARGET_COUNT_OK only for one target. Any other count invokes
-- JSON_EXTRACT with invalid JSON, causing a statement error. The UPDATE also
-- repeats this count check, so error-continuing runners cannot seed 0 or many rows.
SELECT CASE
  WHEN COUNT(*) = 1 THEN 'CI_BUDGET_TARGET_COUNT_OK'
  ELSE JSON_EXTRACT(CONCAT('CI_BUDGET_TARGET_COUNT_INVALID:', COUNT(*)), '$')
END AS seed_guard
FROM cost_budgets
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31';

UPDATE cost_budgets
SET cost_basis = 'net_cost'
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31'
  AND 1 = (
    SELECT COUNT(*)
    FROM (
      SELECT id
      FROM cost_budgets
      WHERE vendor = 'tencent'
        AND LOWER(TRIM(platform)) = 'cicd'
        AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
        AND budget_name = 'PingCAP CICD H2 Tencent 2026'
        AND period_start_date = '2026-09-01'
        AND period_end_date = '2027-03-31'
      GROUP BY id
    ) AS ci_budget_target
  );
