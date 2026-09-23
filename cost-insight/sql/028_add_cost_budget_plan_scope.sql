-- Add the shared budget-plan scope columns before publishing plan-scoped budgets.
-- Plan writers must populate verified membership and platform values; this migration
-- intentionally does not infer JSON membership from legacy account_id values.
ALTER TABLE cost_budgets
  ADD COLUMN IF NOT EXISTS accounts JSON NULL AFTER account_id,
  ADD COLUMN IF NOT EXISTS projects JSON NULL AFTER accounts,
  ADD COLUMN IF NOT EXISTS team VARCHAR(255) NULL AFTER projects,
  ADD COLUMN IF NOT EXISTS platform VARCHAR(16) NULL AFTER team;
