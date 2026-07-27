-- Keep dashboard cost reads bounded by source and date when TiDB's optimizer
-- would otherwise choose broad scans under concurrent load.
CREATE INDEX IF NOT EXISTS idx_cost_attribution_source_date_employee
  ON cost_attribution_daily (vendor, account_id, usage_date, employee_id);

CREATE INDEX IF NOT EXISTS idx_cost_unmatched_source_date_namespace
  ON cost_unmatched_resource_daily (vendor, account_id, usage_date, namespace);
