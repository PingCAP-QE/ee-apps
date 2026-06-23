ALTER TABLE cost_raw_details
  ADD COLUMN IF NOT EXISTS target_branch VARCHAR(255) NULL AFTER repo;

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS target_branch VARCHAR(255) NULL AFTER repo;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS target_branch VARCHAR(255) NULL AFTER repo;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS target_branch VARCHAR(255) NULL AFTER repo;

CREATE INDEX IF NOT EXISTS idx_cost_attribution_daily_branch
  ON cost_attribution_daily (usage_date, target_branch);
