-- Preserve sub-cent GCP billing amounts until Dashboard aggregation.
-- BigQuery detailed export contains high-cardinality resource facts whose
-- individual costs are often below one cent.
-- DECIMAL(16,9) retains the existing eight-byte storage footprint while
-- supporting amounts below $10,000,000 per fact.

ALTER TABLE cost_bq_export_summary_daily
  MODIFY COLUMN list_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN effective_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN credit_amount DECIMAL(16, 9) NULL,
  MODIFY COLUMN net_cost DECIMAL(16, 9) NULL;

ALTER TABLE cost_attribution_daily
  MODIFY COLUMN list_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN effective_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN credit_amount DECIMAL(16, 9) NULL,
  MODIFY COLUMN net_cost DECIMAL(16, 9) NULL;

ALTER TABLE cost_kubernetes_workload_allocation_daily
  MODIFY COLUMN source_node_list_cost DECIMAL(16, 9) NOT NULL,
  MODIFY COLUMN list_cost DECIMAL(16, 9) NOT NULL;

ALTER TABLE cost_kubernetes_workload_allocation_source_daily
  MODIFY COLUMN source_list_cost DECIMAL(16, 9) NOT NULL;

ALTER TABLE cost_allocation_daily
  MODIFY COLUMN list_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN effective_cost DECIMAL(16, 9) NULL,
  MODIFY COLUMN credit_amount DECIMAL(16, 9) NULL,
  MODIFY COLUMN net_cost DECIMAL(16, 9) NULL;
