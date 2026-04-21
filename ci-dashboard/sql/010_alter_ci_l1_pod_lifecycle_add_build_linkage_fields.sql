ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS build_system VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN' AFTER pod_uid;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS jenkins_build_url_key VARCHAR(512) NULL AFTER build_system;

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_build_system_time
  ON ci_l1_pod_lifecycle (build_system, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_build_key_time
  ON ci_l1_pod_lifecycle (source_prow_job_id, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_lifecycle_jenkins_build_key
  ON ci_l1_pod_lifecycle (jenkins_build_url_key);
