ALTER TABLE cost_budgets
  ADD COLUMN IF NOT EXISTS cost_basis VARCHAR(16) NOT NULL DEFAULT 'list_cost'
  AFTER budget_amount;

UPDATE cost_budgets
SET cost_basis = 'net_cost'
WHERE vendor = 'tencent'
  AND LOWER(TRIM(platform)) = 'cicd'
  AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
  AND budget_name = 'PingCAP CICD H2 Tencent 2026'
  AND period_start_date = '2026-09-01'
  AND period_end_date = '2027-03-31';
