-- Apply manually only after the compatibility-removal release has run for the
-- observation window described in docs/cost-schema-retirement-design.md.
-- Image deployment copies this file but does not execute it.

DROP TABLE IF EXISTS cost_unattached_ebs_volume_daily;
DROP TABLE IF EXISTS cost_roster_aliases;

ALTER TABLE cost_sources
  DROP INDEX IF EXISTS idx_cost_sources_billing_account;

ALTER TABLE cost_budgets
  DROP INDEX IF EXISTS idx_cost_budgets_group,
  DROP INDEX IF EXISTS idx_cost_budgets_manager,
  DROP INDEX IF EXISTS idx_cost_budgets_repo;

ALTER TABLE cost_bq_export_summary_daily
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_service,
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_region,
  DROP INDEX IF EXISTS idx_cost_bq_export_summary_export_partition;

ALTER TABLE cost_unmatched_resource_daily
  DROP INDEX IF EXISTS idx_cost_unmatched_resource_repo,
  DROP INDEX IF EXISTS idx_cost_unmatched_resource_resource_name;

DROP TABLE IF EXISTS cost_raw_details;
