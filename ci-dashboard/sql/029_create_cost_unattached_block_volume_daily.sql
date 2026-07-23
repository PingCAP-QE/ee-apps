CREATE TABLE IF NOT EXISTS cost_unattached_block_volume_daily (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  snapshot_date DATE NOT NULL,
  vendor VARCHAR(32) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  region VARCHAR(64) NOT NULL,
  availability_zone VARCHAR(64) NULL,
  volume_id VARCHAR(256) NOT NULL,
  state VARCHAR(32) NOT NULL,
  size_gib DECIMAL(20, 2) NULL,
  tags_json JSON NULL,
  owner VARCHAR(255) NULL,
  owner_source VARCHAR(64) NULL,
  first_seen_available DATE NOT NULL,
  source_created_at DATETIME NULL,
  observed_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_cost_unattached_block_volume_daily_snapshot (
    snapshot_date,
    vendor,
    account_id,
    region,
    volume_id
  ),
  KEY idx_cost_unattached_block_volume_daily_latest (
    vendor,
    snapshot_date,
    state,
    account_id,
    region,
    volume_id
  ),
  KEY idx_cost_unattached_block_volume_daily_volume (
    vendor,
    account_id,
    region,
    volume_id,
    snapshot_date
  )
);
