-- V3 explicit opt-in migration. No DROP/ALTER of deployed tables.
-- All timestamps are UTC; usage_date is a Beijing date.

CREATE TABLE IF NOT EXISTS machine_utilization_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(32) NOT NULL,
  region VARCHAR(64) NOT NULL,
  cluster_id VARCHAR(128) NOT NULL,
  machine_id VARCHAR(255) NOT NULL,
  usage_date DATE NOT NULL,
  resource_class VARCHAR(32) NOT NULL,
  cpu_percent_sum DOUBLE NOT NULL,
  cpu_sample_minutes INTEGER NOT NULL,
  cpu_peak_percent DOUBLE,
  memory_percent_sum DOUBLE NOT NULL,
  memory_sample_minutes INTEGER NOT NULL,
  memory_peak_percent DOUBLE,
  PRIMARY KEY (id),
  CONSTRAINT uq_machine_utilization_day UNIQUE (account_id, region, cluster_id, machine_id, usage_date)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_machine_utilization_date ON machine_utilization_daily (account_id, usage_date);

CREATE TABLE IF NOT EXISTS pod_utilization_context (
  id BIGINT NOT NULL AUTO_INCREMENT,
  context_key VARCHAR(64) NOT NULL,
  account_id VARCHAR(32) NOT NULL,
  cluster_id VARCHAR(128) NOT NULL,
  namespace VARCHAR(128) NOT NULL,
  pod_uid VARCHAR(255) NOT NULL,
  pod_name VARCHAR(255) NOT NULL,
  node_instance_id VARCHAR(255),
  cpu_request_cores DOUBLE,
  memory_request_bytes DOUBLE,
  labels_json JSON NOT NULL,
  observed_from DATETIME(6) NOT NULL,
  observed_until DATETIME(6) NOT NULL,
  closed_at DATETIME(6),
  planned_until DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE (context_key)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_pod_context_expiry ON pod_utilization_context (expires_at, id);

CREATE INDEX ix_pod_context_identity ON pod_utilization_context (account_id, cluster_id, pod_uid);

CREATE TABLE IF NOT EXISTS utilization_task (
  id VARCHAR(64) NOT NULL,
  account_id VARCHAR(32) NOT NULL,
  kind VARCHAR(16) NOT NULL,
  context_id BIGINT,
  payload_json JSON NOT NULL,
  window_start DATETIME(6) NOT NULL,
  window_end DATETIME(6) NOT NULL,
  due_at DATETIME(6) NOT NULL,
  deadline_at DATETIME(6) NOT NULL,
  status VARCHAR(16) NOT NULL,
  lease_token VARCHAR(64),
  lease_until DATETIME(6),
  dispatched_at DATETIME(6),
  PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_utilization_task_due ON utilization_task (account_id, status, due_at);

CREATE INDEX ix_utilization_task_expiry ON utilization_task (deadline_at, id);

CREATE INDEX ix_utilization_task_context ON utilization_task (context_id);

CREATE TABLE IF NOT EXISTS workload_utilization_daily (
  account_id VARCHAR(32) NOT NULL,
  usage_date DATE NOT NULL,
  group_key VARCHAR(64) NOT NULL,
  labels_json JSON NOT NULL,
  cpu_used DOUBLE NOT NULL,
  cpu_requested DOUBLE NOT NULL,
  memory_used DOUBLE NOT NULL,
  memory_requested DOUBLE NOT NULL,
  cpu_matched_pod_minutes BIGINT NOT NULL,
  memory_matched_pod_minutes BIGINT NOT NULL,
  expected_pod_minutes BIGINT NOT NULL,
  PRIMARY KEY (account_id, usage_date, group_key)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
