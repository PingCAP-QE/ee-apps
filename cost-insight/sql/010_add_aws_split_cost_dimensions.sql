ALTER TABLE cost_sources
  ADD COLUMN IF NOT EXISTS source_table VARCHAR(512) NULL AFTER display_name,
  ADD COLUMN IF NOT EXISTS source_schema_version VARCHAR(64) NULL AFTER source_table,
  ADD COLUMN IF NOT EXISTS source_available_from DATE NULL AFTER source_schema_version;

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS source_schema_version VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS namespace VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL,
  ADD COLUMN IF NOT EXISTS owner VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS parent_resource_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL,
  ADD COLUMN IF NOT EXISTS owner VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS project VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS service_exec_id VARCHAR(255) NULL;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS namespace VARCHAR(255) NULL,
  ADD COLUMN IF NOT EXISTS workload_name VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS workload_type VARCHAR(128) NULL;

CREATE INDEX IF NOT EXISTS idx_cost_attribution_source_scope_date
  ON cost_attribution_daily (vendor, account_id, source_allocation_scope, usage_date);

CREATE TABLE IF NOT EXISTS cost_aws_parent_residual_allocation_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  parent_resource_id VARCHAR(512) NOT NULL,
  pod_resource_id VARCHAR(512) NOT NULL,
  namespace VARCHAR(255) NULL,
  workload_name VARCHAR(512) NULL,
  workload_type VARCHAR(128) NULL,
  owner VARCHAR(255) NULL,
  service VARCHAR(255) NULL,
  project VARCHAR(255) NULL,
  service_exec_id VARCHAR(255) NULL,
  source_pod_split_list_cost DECIMAL(16,2) NOT NULL,
  parent_direct_list_cost DECIMAL(16,2) NOT NULL,
  parent_residual_list_cost DECIMAL(16,2) NOT NULL,
  allocation_weight DECIMAL(32,16) NOT NULL,
  derived_parent_residual_list_cost DECIMAL(16,2) NOT NULL,
  allocation_origin VARCHAR(64) NOT NULL,
  allocation_method VARCHAR(128) NOT NULL,
  allocation_version VARCHAR(64) NOT NULL,
  parent_input_hash CHAR(64) NOT NULL,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_aws_parent_residual_allocation (
    usage_date,
    vendor,
    account_id,
    parent_resource_id,
    pod_resource_id,
    allocation_version
  ),
  KEY idx_cost_aws_parent_residual_parent (
    vendor,
    account_id,
    usage_date,
    parent_resource_id
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
