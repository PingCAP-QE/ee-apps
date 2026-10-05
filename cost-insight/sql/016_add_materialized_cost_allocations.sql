-- Non-destructive allocation schema. This migration does not depend on the
-- destructive table drops in 015 and may be applied first during staged rollout.
ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS cluster_name VARCHAR(255) NULL AFTER source_allocation_scope,
  ADD COLUMN IF NOT EXISTS cluster_location VARCHAR(128) NULL AFTER cluster_name,
  ADD COLUMN IF NOT EXISTS kubernetes_cost_class VARCHAR(32) NULL AFTER cluster_location,
  ADD COLUMN IF NOT EXISTS kubernetes_residual_type VARCHAR(32) NULL AFTER kubernetes_cost_class,
  ADD COLUMN IF NOT EXISTS kubernetes_cost_component VARCHAR(32) NULL AFTER kubernetes_residual_type;

CREATE INDEX IF NOT EXISTS idx_cost_summary_kubernetes_source
  ON cost_bq_export_summary_daily (
    vendor,
    account_id,
    usage_date,
    kubernetes_cost_class
  );

CREATE TABLE IF NOT EXISTS cost_allocation_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  basis_key VARCHAR(32) NOT NULL,
  allocation_version VARCHAR(64) NOT NULL,
  allocation_stage VARCHAR(32) NOT NULL,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  service_name VARCHAR(255) NULL,
  sku_name VARCHAR(255) NULL,
  usage_type VARCHAR(255) NULL,
  cost_driver_key VARCHAR(64) NULL,
  region VARCHAR(128) NULL,
  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  target_branch VARCHAR(255) NULL,
  resource_name VARCHAR(512) NULL,
  vendor_tags_json JSON NULL,
  source_allocation_scope VARCHAR(32) NOT NULL DEFAULT 'direct',
  namespace VARCHAR(255) NULL,
  workload_name VARCHAR(512) NULL,
  workload_type VARCHAR(128) NULL,
  author VARCHAR(255) NULL,
  owner VARCHAR(255) NULL,
  service VARCHAR(255) NULL,
  project VARCHAR(255) NULL,
  service_exec_id VARCHAR(255) NULL,
  attribution_key VARCHAR(255) NULL,
  attribution_source VARCHAR(64) NOT NULL,
  attribution_status VARCHAR(64) NOT NULL,
  allocate_method VARCHAR(32) NULL,
  employee_id BIGINT NULL,
  group_id BIGINT NULL,
  manager_id BIGINT NULL,
  usage_seconds DECIMAL(20,2) NULL,
  list_cost DECIMAL(16,2) NULL,
  effective_cost DECIMAL(16,2) NULL,
  credit_amount DECIMAL(16,2) NULL,
  net_cost DECIMAL(16,2) NULL,
  source_rows BIGINT NOT NULL DEFAULT 0,
  source_summary_row_hash CHAR(64) NULL,
  source_fact_hash CHAR(64) NOT NULL,
  source_owner VARCHAR(255) NULL,
  source_group_id BIGINT NULL,
  source_manager_id BIGINT NULL,
  target_group_id BIGINT NULL,
  target_manager_id BIGINT NULL,
  allocation_scope VARCHAR(32) NOT NULL,
  allocation_method VARCHAR(128) NOT NULL,
  allocation_weight DECIMAL(32,16) NOT NULL,
  roster_resolved_at DATETIME NOT NULL,
  dimension_hash CHAR(64) NOT NULL,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_allocation_daily_versioned (
    basis_key,
    allocation_version,
    usage_date,
    dimension_hash
  ),
  KEY idx_cost_allocation_serving (
    basis_key,
    allocation_version,
    vendor,
    account_id,
    usage_date
  ),
  KEY idx_cost_allocation_group (basis_key, allocation_version, usage_date, group_id),
  KEY idx_cost_allocation_manager (basis_key, allocation_version, usage_date, manager_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cost_allocation_publication (
  publication_name VARCHAR(32) NOT NULL,
  active_allocation_version VARCHAR(64) NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (publication_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
