ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER target_branch;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER target_branch;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS vendor_tags_json JSON NULL AFTER resource_name,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL AFTER owner,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL AFTER service,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL AFTER project,
  ADD COLUMN IF NOT EXISTS allocate_method VARCHAR(32) NULL AFTER attribution_status;

CREATE INDEX IF NOT EXISTS idx_cost_attribution_daily_project
  ON cost_attribution_daily (usage_date, project);
