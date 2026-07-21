-- usage_type and cost_driver_key are derived display dimensions.
-- They do not change cost_bq_export_summary_daily.source_row_hash, so this
-- migration does not require partition replacement.

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS usage_type VARCHAR(255) NULL AFTER sku_name,
  ADD COLUMN IF NOT EXISTS cost_driver_key VARCHAR(64) NULL AFTER usage_type;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS usage_type VARCHAR(255) NULL AFTER sku_name,
  ADD COLUMN IF NOT EXISTS cost_driver_key VARCHAR(64) NULL AFTER usage_type;
