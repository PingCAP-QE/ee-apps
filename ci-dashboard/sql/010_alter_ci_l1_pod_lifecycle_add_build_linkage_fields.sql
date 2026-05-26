ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS build_system VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN' AFTER pod_uid;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS normalized_build_url VARCHAR(1024) NULL AFTER source_prow_job_id;

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_build_system_time
  ON ci_l1_pod_lifecycle (build_system, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_build_key_time
  ON ci_l1_pod_lifecycle (source_prow_job_id, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_normalized_build_url
  ON ci_l1_pod_lifecycle (normalized_build_url(768));
