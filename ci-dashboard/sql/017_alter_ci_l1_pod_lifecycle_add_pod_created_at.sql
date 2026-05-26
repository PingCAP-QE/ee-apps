ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_created_at DATETIME NULL AFTER metadata_observed_at;
