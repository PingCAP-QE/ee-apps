ALTER TABLE ci_l1_pod_lifecycle
  DROP INDEX IF EXISTS idx_ci_l1_pod_lifecycle_cloud_time;

ALTER TABLE ci_l1_pod_lifecycle
  DROP COLUMN IF EXISTS cloud_phase;
