INSERT INTO cost_sources (
  vendor,
  account_id,
  billing_account_id,
  display_name,
  source_table,
  source_schema_version,
  source_available_from,
  purpose,
  is_active
) VALUES (
  'aws',
  '380838443567',
  '317766989874',
  'tidb-cloud-prod-us-west-2-f04',
  'gcp-digital-bi.aws_prod_billing.aws_prod_billing_data',
  'aws_tidb_cloud_f04_v1',
  '2026-09-02',
  'TiDB Cloud production us-west-2 f04',
  0
)
ON DUPLICATE KEY UPDATE
  billing_account_id = VALUES(billing_account_id),
  display_name = VALUES(display_name),
  source_table = VALUES(source_table),
  source_schema_version = VALUES(source_schema_version),
  source_available_from = VALUES(source_available_from),
  purpose = VALUES(purpose),
  is_active = VALUES(is_active),
  updated_at = CURRENT_TIMESTAMP;
