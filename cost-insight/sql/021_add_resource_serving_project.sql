ALTER TABLE cost_resource_serving_daily
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL AFTER manager_id;

CREATE INDEX IF NOT EXISTS idx_resource_serving_project_date
  ON cost_resource_serving_daily (basis_key, vendor, account_id, project, usage_date);
