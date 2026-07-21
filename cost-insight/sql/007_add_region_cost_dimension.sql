-- Operational note:
-- Region is part of cost_bq_export_summary_daily.source_row_hash, and
-- cost_raw_details.source_row_hash already includes region. After changing
-- ingestion-time region values, historical summary rows must be rebuilt with
-- sync-*-billing-summary --replace-existing-partitions before attribution is
-- refreshed. If raw history is re-imported or used for refine backfills, rebuild
-- the matching raw usage-date window with sync-gcp-billing-export
-- --replace-existing-dates. Incremental sync alone cannot overwrite old
-- regionless hashes and will double count rows if old and new hashes remain in
-- the same partition or usage-date window.

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS region VARCHAR(128) NULL AFTER sku_name;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS region VARCHAR(128) NULL AFTER sku_name;

CREATE INDEX IF NOT EXISTS idx_cost_bq_export_summary_region
  ON cost_bq_export_summary_daily (usage_date, region);

CREATE INDEX IF NOT EXISTS idx_cost_attribution_daily_region
  ON cost_attribution_daily (usage_date, region);
