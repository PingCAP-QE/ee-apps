-- GKE billing exports have many source rows per cluster/day/component. Keep the
-- source-to-group mapping separate from the workload allocation facts so the
-- dashboard can replace only fully reconciled source costs without expanding
-- every source row by every workload.
ALTER TABLE cost_kubernetes_workload_allocation_daily
  ADD COLUMN IF NOT EXISTS allocation_group_hash CHAR(64) NULL AFTER source_summary_row_hash;

CREATE INDEX IF NOT EXISTS idx_cost_kubernetes_allocation_group
  ON cost_kubernetes_workload_allocation_daily (
    vendor,
    account_id,
    usage_date,
    allocation_group_hash
  );

CREATE TABLE IF NOT EXISTS cost_kubernetes_workload_allocation_source_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  usage_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  source_summary_row_hash CHAR(64) NOT NULL,
  allocation_group_hash CHAR(64) NOT NULL,
  source_list_cost DECIMAL(16,2) NOT NULL,
  allocation_version VARCHAR(64) NOT NULL,
  calculated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cost_kubernetes_allocation_source (
    vendor,
    account_id,
    usage_date,
    source_summary_row_hash
  ),
  KEY idx_cost_kubernetes_allocation_source_group (
    vendor,
    account_id,
    usage_date,
    allocation_group_hash
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
