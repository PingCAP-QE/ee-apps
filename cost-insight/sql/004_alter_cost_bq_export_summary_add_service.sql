ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS service_name VARCHAR(255) NULL AFTER usage_date,
  ADD COLUMN IF NOT EXISTS sku_name VARCHAR(255) NULL AFTER service_name;

CREATE INDEX IF NOT EXISTS idx_cost_bq_export_summary_service
  ON cost_bq_export_summary_daily (usage_date, service_name);
