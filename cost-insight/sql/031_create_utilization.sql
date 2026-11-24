-- Explicit opt-in migration; no cloud/production application is performed by the job.
-- All business timestamps are UTC-naive. expires_at is usage-week Monday
-- Asia/Shanghai +14 days, never updated_at +14 days. Guard lives in cost_job_state.
CREATE TABLE IF NOT EXISTS cost_utilization_daily (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vendor VARCHAR(16) NOT NULL,
  account_id VARCHAR(32) NOT NULL,
  usage_date DATE NOT NULL,
  week_start DATE NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  scope VARCHAR(24) NOT NULL,
  resource_key VARCHAR(64) NOT NULL,
  resource_class VARCHAR(64) NOT NULL,
  resource_spec VARCHAR(255) NOT NULL DEFAULT '',
  repo VARCHAR(255) NOT NULL DEFAULT 'unknown_repo',
  job_name VARCHAR(512) NOT NULL DEFAULT 'unknown_job',
  namespace VARCHAR(128) NOT NULL DEFAULT '',
  region VARCHAR(64) NOT NULL DEFAULT '',
  metrics_json JSON NOT NULL,
  cutoff DATETIME(6) NOT NULL,
  evidence_cutoff DATETIME(6) NULL,
  status VARCHAR(24) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_utilization_daily (vendor, account_id, usage_date, scope, resource_key),
  KEY ix_utilization_daily_expiry (expires_at, id),
  KEY ix_utilization_daily_week (vendor, account_id, week_start, scope, resource_class)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE IF NOT EXISTS cost_utilization_weekly (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vendor VARCHAR(16) NOT NULL,
  account_id VARCHAR(32) NOT NULL,
  week_start DATE NOT NULL,
  resource_class VARCHAR(64) NOT NULL,
  metric VARCHAR(32) NOT NULL,
  contract_json JSON NOT NULL,
  values_json JSON NOT NULL,
  status VARCHAR(32) NOT NULL,
  quality_checkpoint_at DATETIME(6) NULL,
  finalized_at DATETIME(6) NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_utilization_weekly (vendor, account_id, week_start, resource_class, metric)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE TABLE IF NOT EXISTS cost_utilization_resource_observation (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vendor VARCHAR(16) NOT NULL,
  account_id VARCHAR(32) NOT NULL,
  week_start DATE NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  resource_type VARCHAR(8) NOT NULL,
  resource_id VARCHAR(255) NOT NULL,
  cluster VARCHAR(128) NOT NULL DEFAULT '',
  namespace VARCHAR(128) NOT NULL DEFAULT '',
  name VARCHAR(255) NOT NULL DEFAULT '',
  provider_id VARCHAR(255) NOT NULL DEFAULT '',
  observed_from DATETIME(6) NOT NULL,
  observed_until DATETIME(6) NULL,
  last_seen DATETIME(6) NOT NULL,
  fingerprint VARCHAR(64) NOT NULL,
  attributes_json JSON NOT NULL,
  UNIQUE KEY uq_utilization_observation
    (vendor, account_id, week_start, resource_type, resource_id, observed_from),
  KEY ix_utilization_observation_expiry (expires_at, id),
  KEY ix_utilization_observation_interval (vendor, account_id, week_start, resource_type)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
