-- Apply only after the F04-aware warehouse image and dedicated CronJob are ready,
-- with no legacy F04 writer active. This is a source-route update, not a backfill.
-- Retain the specialized profile: its usedby boundary must never become account-wide.
UPDATE cost_sources
SET source_table = 'warehouse-f80fbbe8e8.raw_aws_billing.cur_line_items'
WHERE vendor = 'aws'
  AND account_id = '380838443567'
  AND is_active = 1
  AND source_schema_version = 'aws_tidb_cloud_f04_v1'
  AND source_table = 'gcp-digital-bi.aws_prod_billing.aws_prod_billing_data';

-- This SELECT must return exactly one active warehouse-routed F04 source before
-- starting any new writer; a zero-row UPDATE alone is not proof of migration.
SELECT vendor, account_id, is_active, source_table, source_schema_version,
       source_available_from
FROM cost_sources
WHERE vendor = 'aws' AND account_id = '380838443567';
