-- Run the preflight in docs/cost-budget-scope-design.md before applying.
ALTER TABLE cost_budgets
  MODIFY COLUMN vendor VARCHAR(32) NULL,
  MODIFY COLUMN account_id VARCHAR(128) NULL,
  ADD COLUMN scope_key CHAR(64) NULL AFTER account_id;

UPDATE cost_budgets
SET scope_key = SHA2(CONCAT('account', CHAR(0), vendor, CHAR(0), account_id), 256);

ALTER TABLE cost_budgets
  MODIFY COLUMN scope_key CHAR(64) NOT NULL,
  DROP INDEX uk_cost_budgets_scope,
  ADD UNIQUE KEY uk_cost_budgets_scope_period (
    scope_key,
    period_start_date,
    period_end_date
  );
