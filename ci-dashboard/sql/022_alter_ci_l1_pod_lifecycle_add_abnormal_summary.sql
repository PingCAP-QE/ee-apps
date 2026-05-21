ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS abnormal_reason VARCHAR(64) NULL AFTER pod_created_at;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS abnormal_message TEXT NULL AFTER abnormal_reason;
