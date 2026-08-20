-- Preserve the billable resource identity in the summary path. In particular,
-- GKE PersistentVolume names (pvc-<uuid>) are needed for PVC-to-pod attribution.
ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS resource_name VARCHAR(512) NULL AFTER target_branch;

CREATE INDEX IF NOT EXISTS idx_cost_bq_export_summary_resource
  ON cost_bq_export_summary_daily (vendor, account_id, usage_date, resource_name(255));
