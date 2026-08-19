CREATE TABLE IF NOT EXISTS cost_kubernetes_workload_allocation_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  cluster_name VARCHAR(255) NULL,
  cluster_location VARCHAR(128) NULL,
  allocation_scope VARCHAR(32) NOT NULL,
  cost_component VARCHAR(32) NOT NULL,
  namespace VARCHAR(255) NULL,
  workload_name VARCHAR(512) NULL,
  workload_type VARCHAR(128) NULL,
  author VARCHAR(255) NULL,
  org VARCHAR(255) NULL,
  repo VARCHAR(255) NULL,
  target_branch VARCHAR(255) NULL,
  allocation_weight DECIMAL(32,16) NOT NULL,
  source_node_list_cost DECIMAL(16,2) NOT NULL,
  list_cost DECIMAL(16,2) NOT NULL,
  allocation_method VARCHAR(128) NOT NULL,
  allocation_version VARCHAR(64) NOT NULL,
  dimension_hash CHAR(64) NOT NULL,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_kubernetes_workload_allocation (
    usage_date,
    dimension_hash
  ),
  KEY idx_cost_kubernetes_allocation_source_date (
    vendor,
    account_id,
    usage_date,
    allocation_scope
  ),
  KEY idx_cost_kubernetes_allocation_branch (
    vendor,
    account_id,
    target_branch,
    usage_date
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
