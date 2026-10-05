-- Currency propagation for multi-currency (CNY Tencent) cost reporting.
-- Existing rows keep the USD default; Tencent import writes CNY.
ALTER TABLE cost_bq_export_summary_daily
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER net_cost;

ALTER TABLE cost_attribution_daily
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER net_cost;

ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER net_cost;

ALTER TABLE cost_allocation_daily
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER net_cost;

ALTER TABLE cost_resource_serving_daily
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER net_cost;

ALTER TABLE cost_resource_serving_publication
  ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'USD' AFTER total_list_cost;
