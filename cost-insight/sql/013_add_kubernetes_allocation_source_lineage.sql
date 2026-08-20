-- A dashboard allocation view replaces only a fully reconciled source billing row.
-- Keep that source identity on both the displayed attribution row and every
-- Kubernetes allocation fact derived from it.
ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS source_summary_row_hash CHAR(64) NULL AFTER source_rows;

ALTER TABLE cost_kubernetes_workload_allocation_daily
  ADD COLUMN IF NOT EXISTS source_summary_row_hash CHAR(64) NULL AFTER dimension_hash;

CREATE INDEX IF NOT EXISTS idx_cost_attribution_summary_lineage
  ON cost_attribution_daily (vendor, account_id, usage_date, source_summary_row_hash);

CREATE INDEX IF NOT EXISTS idx_cost_kubernetes_allocation_summary_lineage
  ON cost_kubernetes_workload_allocation_daily (
    vendor,
    account_id,
    usage_date,
    source_summary_row_hash
  );
