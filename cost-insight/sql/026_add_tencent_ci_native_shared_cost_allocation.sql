-- Tencent CI native shared-cost allocation V1. Apply after 025.
-- This migration deliberately contains no Tencent classification rules beyond an
-- empty sealed baseline: unknown Tencent code tuples must remain unclassified.

ALTER TABLE cost_sources
  ADD COLUMN IF NOT EXISTS attribution_write_mode VARCHAR(64) NOT NULL DEFAULT 'direct_summary'
    AFTER source_available_from;

UPDATE cost_sources
SET attribution_write_mode = 'tencent_ci_published_terminal'
WHERE vendor = 'tencent' AND account_id = '100050658403';

ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS tencent_business_code VARCHAR(128) NULL AFTER source_row_hash,
  ADD COLUMN IF NOT EXISTS tencent_product_code VARCHAR(128) NULL AFTER tencent_business_code,
  ADD COLUMN IF NOT EXISTS tencent_component_code VARCHAR(128) NULL AFTER tencent_product_code,
  ADD COLUMN IF NOT EXISTS tencent_item_code VARCHAR(128) NULL AFTER tencent_component_code,
  ADD COLUMN IF NOT EXISTS tencent_cost_class VARCHAR(32) NULL AFTER tencent_item_code,
  ADD COLUMN IF NOT EXISTS tencent_classification_version VARCHAR(64) NULL AFTER tencent_cost_class,
  ADD COLUMN IF NOT EXISTS tencent_import_generation CHAR(36) NULL AFTER tencent_classification_version;

CREATE INDEX IF NOT EXISTS idx_tencent_summary_classification
  ON cost_bq_export_summary_daily (
    vendor, account_id, usage_date, tencent_cost_class, tencent_classification_version
  );

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS allocation_version VARCHAR(64) NULL AFTER source_summary_row_hash,
  ADD COLUMN IF NOT EXISTS classification_version VARCHAR(64) NULL AFTER allocation_version,
  ADD COLUMN IF NOT EXISTS weight_model VARCHAR(64) NULL AFTER classification_version,
  ADD COLUMN IF NOT EXISTS weight_version VARCHAR(64) NULL AFTER weight_model,
  ADD COLUMN IF NOT EXISTS allocation_weight DECIMAL(32,16) NULL AFTER weight_version,
  ADD COLUMN IF NOT EXISTS source_pool_key CHAR(64) NULL AFTER allocation_weight;

CREATE INDEX IF NOT EXISTS idx_tencent_attribution_allocation
  ON cost_attribution_daily (vendor, account_id, usage_date, allocation_version);

CREATE TABLE IF NOT EXISTS tencent_cost_classification_rule_set (
  classification_version VARCHAR(64) NOT NULL,
  rules_json JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  reviewed_by VARCHAR(255) NULL,
  published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (classification_version),
  UNIQUE KEY uk_tencent_classification_content_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- SHA-256 of canonical JSON []: the safe default classifies every tuple as
-- unclassified, so it cannot be materialized or published by accident.
INSERT IGNORE INTO tencent_cost_classification_rule_set (
  classification_version, rules_json, content_hash, reviewed_by
) VALUES (
  'empty-v1', JSON_ARRAY(),
  '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
  'migration-safe-default'
);

CREATE TABLE IF NOT EXISTS tencent_ci_identity_alias_rule_set (
  alias_version VARCHAR(64) NOT NULL,
  aliases_json JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  reviewed_by VARCHAR(255) NULL,
  published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (alias_version),
  UNIQUE KEY uk_tencent_alias_content_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO tencent_ci_identity_alias_rule_set (
  alias_version, aliases_json, content_hash, reviewed_by
) VALUES (
  'empty-v1', JSON_OBJECT(),
  '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
  'migration-safe-default'
);

CREATE TABLE IF NOT EXISTS tencent_billing_import_partition (
  account_id VARCHAR(128) NOT NULL,
  bill_day DATE NOT NULL,
  import_generation CHAR(36) NOT NULL,
  is_complete TINYINT(1) NOT NULL DEFAULT 0,
  usage_dates_json JSON NOT NULL,
  source_fingerprint CHAR(64) NULL,
  completed_at DATETIME NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, bill_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_daily_state (
  account_id VARCHAR(128) NOT NULL,
  usage_date DATE NOT NULL,
  ledger_fingerprint CHAR(64) NULL,
  build_fingerprint CHAR(64) NULL,
  active_allocation_version VARCHAR(64) NULL,
  is_stale TINYINT(1) NOT NULL DEFAULT 1,
  stale_reason VARCHAR(64) NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_roster_snapshot (
  snapshot_id CHAR(64) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  roster_json JSON NOT NULL,
  resolved_at DATETIME NOT NULL,
  PRIMARY KEY (snapshot_id),
  UNIQUE KEY uk_tencent_roster_snapshot_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_allocation_manifest (
  allocation_version VARCHAR(64) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  classification_version VARCHAR(64) NOT NULL,
  classification_hash CHAR(64) NOT NULL,
  alias_version VARCHAR(64) NOT NULL,
  alias_hash CHAR(64) NOT NULL,
  roster_snapshot_id CHAR(64) NOT NULL,
  weight_model VARCHAR(64) NOT NULL,
  weight_version VARCHAR(64) NOT NULL,
  algorithm_version VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (allocation_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_allocation_day (
  allocation_version VARCHAR(64) NOT NULL,
  usage_date DATE NOT NULL,
  status VARCHAR(16) NOT NULL,
  ledger_fingerprint CHAR(64) NOT NULL,
  build_fingerprint CHAR(64) NOT NULL,
  pool_fingerprint CHAR(64) NOT NULL,
  projection_row_count BIGINT NOT NULL DEFAULT 0,
  statistics_json JSON NOT NULL,
  validated_at DATETIME NULL,
  published_at DATETIME NULL,
  failure_reason TEXT NULL,
  PRIMARY KEY (allocation_version, usage_date),
  KEY idx_tencent_allocation_day_status (status, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_allocation_projection (
  id BIGINT NOT NULL AUTO_INCREMENT,
  allocation_version VARCHAR(64) NOT NULL,
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
  source_allocation_scope VARCHAR(32) NOT NULL,
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
  list_cost DECIMAL(16,9) NULL,
  effective_cost DECIMAL(16,9) NULL,
  credit_amount DECIMAL(16,9) NULL,
  net_cost DECIMAL(16,9) NULL,
  currency CHAR(3) NOT NULL,
  source_rows BIGINT NOT NULL,
  source_summary_row_hash CHAR(64) NULL,
  classification_version VARCHAR(64) NOT NULL,
  weight_model VARCHAR(64) NOT NULL,
  weight_version VARCHAR(64) NOT NULL,
  allocation_weight DECIMAL(32,16) NULL,
  source_pool_key CHAR(64) NULL,
  dimension_hash CHAR(64) NOT NULL,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tencent_projection_versioned (
    allocation_version, usage_date, dimension_hash
  ),
  KEY idx_tencent_projection_publish (allocation_version, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_allocation_publication (
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  usage_date DATE NOT NULL,
  active_allocation_version VARCHAR(64) NOT NULL,
  ledger_fingerprint CHAR(64) NOT NULL,
  build_fingerprint CHAR(64) NOT NULL,
  published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (vendor, account_id, usage_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tencent_ci_allocation_publication_event (
  id BIGINT NOT NULL AUTO_INCREMENT,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  usage_date DATE NOT NULL,
  allocation_version VARCHAR(64) NOT NULL,
  event_type VARCHAR(16) NOT NULL,
  previous_allocation_version VARCHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_tencent_publication_event (account_id, usage_date, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
