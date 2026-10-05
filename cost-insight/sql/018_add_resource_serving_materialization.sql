-- Published resource-serving projection. Resource detail remains the audit ledger;
-- this table is the bounded Dashboard read model.
ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS region VARCHAR(128) NULL AFTER usage_date,
  ADD COLUMN IF NOT EXISTS source_summary_row_hash CHAR(64) NULL AFTER source_row_hash,
  MODIFY COLUMN list_cost DECIMAL(16,9) NULL,
  MODIFY COLUMN effective_cost DECIMAL(16,9) NULL,
  MODIFY COLUMN credit_amount DECIMAL(16,9) NULL,
  MODIFY COLUMN net_cost DECIMAL(16,9) NULL;

CREATE INDEX IF NOT EXISTS idx_cost_unmatched_resource_summary_lineage
  ON cost_unmatched_resource_daily (vendor, account_id, usage_date, source_summary_row_hash);

CREATE TABLE IF NOT EXISTS cost_resource_serving_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  materialization_version VARCHAR(64) NOT NULL,
  basis_key VARCHAR(32) NOT NULL,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  owner_key CHAR(64) NOT NULL,
  owner VARCHAR(255) NOT NULL DEFAULT '',
  group_id BIGINT NULL,
  manager_id BIGINT NULL,
  target_branch VARCHAR(255) NULL,
  resource_group_key CHAR(64) NOT NULL,
  resource_key CHAR(64) NOT NULL,
  resource_name VARCHAR(512) NOT NULL,
  service_name VARCHAR(255) NULL,
  resource_identity_kind VARCHAR(32) NOT NULL,
  representative_labels_json JSON NULL,
  metadata_variant_count BIGINT NOT NULL DEFAULT 0,
  detail_list_cost DECIMAL(16,9) NOT NULL DEFAULT 0,
  fallback_list_cost DECIMAL(16,9) NOT NULL DEFAULT 0,
  usage_seconds DECIMAL(20,2) NULL,
  list_cost DECIMAL(16,9) NOT NULL,
  effective_cost DECIMAL(16,9) NULL,
  credit_amount DECIMAL(16,9) NULL,
  net_cost DECIMAL(16,9) NULL,
  source_row_count BIGINT NOT NULL DEFAULT 0,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_resource_serving_versioned (
    materialization_version, basis_key, vendor, account_id, usage_date,
    owner_key, resource_key, target_branch
  ),
  KEY idx_resource_serving_owner_date (
    basis_key, vendor, account_id, owner_key, usage_date
  ),
  KEY idx_resource_serving_group_date (
    basis_key, vendor, account_id, group_id, usage_date
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cost_resource_serving_publication (
  basis_key VARCHAR(32) NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  usage_date DATE NOT NULL,
  active_materialization_version VARCHAR(64) NOT NULL,
  source_allocation_version VARCHAR(64) NULL,
  detail_list_cost DECIMAL(16,9) NOT NULL DEFAULT 0,
  total_list_cost DECIMAL(16,9) NOT NULL DEFAULT 0,
  source_row_count BIGINT NOT NULL DEFAULT 0,
  published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  tiflash_ready_at DATETIME NULL,
  PRIMARY KEY (basis_key, vendor, account_id, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
