-- V2 explicit opt-in migration. No DROP/ALTER of deployed tables.
-- V1 tables were never deployed by this PR. All business timestamps are UTC.
-- Machine facts retain one year; Pod contexts/tasks are an active, bounded workset.

CREATE TABLE IF NOT EXISTS machine_utilization_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(32) NOT NULL,
  region VARCHAR(64) NOT NULL,
  cluster_id VARCHAR(128) NOT NULL,
  machine_id VARCHAR(255) NOT NULL,
  usage_date DATE NOT NULL,
  resource_class VARCHAR(32) NOT NULL,
  instance_type VARCHAR(128) NOT NULL,
  cpu_cores DOUBLE,
  memory_bytes DOUBLE,
  cpu_used DOUBLE NOT NULL,
  cpu_capacity DOUBLE NOT NULL,
  cpu_total_used DOUBLE NOT NULL,
  cpu_expected_capacity DOUBLE NOT NULL,
  cpu_expected_seconds DOUBLE NOT NULL,
  cpu_observed_seconds DOUBLE NOT NULL,
  cpu_usage_seconds DOUBLE NOT NULL,
  cpu_unknown_seconds DOUBLE NOT NULL,
  cpu_zero_seconds DOUBLE NOT NULL,
  cpu_peak DOUBLE,
  cpu_peak_at DATETIME(6),
  memory_used DOUBLE NOT NULL,
  memory_capacity DOUBLE NOT NULL,
  memory_total_used DOUBLE NOT NULL,
  memory_expected_capacity DOUBLE NOT NULL,
  memory_expected_seconds DOUBLE NOT NULL,
  memory_observed_seconds DOUBLE NOT NULL,
  memory_usage_seconds DOUBLE NOT NULL,
  memory_unknown_seconds DOUBLE NOT NULL,
  memory_zero_seconds DOUBLE NOT NULL,
  memory_peak DOUBLE,
  memory_peak_at DATETIME(6),
  quality_json JSON NOT NULL,
  cutoff DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
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
  node_query_kind VARCHAR(32) NOT NULL,
  node_query_value VARCHAR(255) NOT NULL,
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
  attempts INTEGER NOT NULL,
  lease_token VARCHAR(64),
  lease_until DATETIME(6),
  dispatched_at DATETIME(6),
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_utilization_task_due ON utilization_task (account_id, status, due_at);

CREATE INDEX ix_utilization_task_expiry ON utilization_task (deadline_at, id);

CREATE TABLE IF NOT EXISTS workload_utilization_daily (
  id BIGINT NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(32) NOT NULL,
  usage_date DATE NOT NULL,
  group_key VARCHAR(64) NOT NULL,
  labels_json JSON NOT NULL,
  cpu_request_cores DOUBLE,
  memory_request_bytes DOUBLE,
  cpu_used DOUBLE NOT NULL,
  cpu_capacity DOUBLE NOT NULL,
  cpu_total_used DOUBLE NOT NULL,
  cpu_expected_capacity DOUBLE NOT NULL,
  cpu_expected_seconds DOUBLE NOT NULL,
  cpu_observed_seconds DOUBLE NOT NULL,
  cpu_usage_seconds DOUBLE NOT NULL,
  cpu_unknown_seconds DOUBLE NOT NULL,
  cpu_zero_seconds DOUBLE NOT NULL,
  memory_used DOUBLE NOT NULL,
  memory_capacity DOUBLE NOT NULL,
  memory_total_used DOUBLE NOT NULL,
  memory_expected_capacity DOUBLE NOT NULL,
  memory_expected_seconds DOUBLE NOT NULL,
  memory_observed_seconds DOUBLE NOT NULL,
  memory_usage_seconds DOUBLE NOT NULL,
  memory_unknown_seconds DOUBLE NOT NULL,
  memory_zero_seconds DOUBLE NOT NULL,
  quality_json JSON NOT NULL,
  cutoff DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uq_workload_utilization_day UNIQUE (account_id, usage_date, group_key)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE INDEX ix_workload_expiry ON workload_utilization_daily (expires_at, id);
