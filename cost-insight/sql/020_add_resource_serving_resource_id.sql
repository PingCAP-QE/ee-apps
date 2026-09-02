ALTER TABLE cost_unmatched_resource_daily
  ADD COLUMN resource_id VARCHAR(1024) NULL AFTER resource_name;

ALTER TABLE cost_resource_serving_daily
  ADD COLUMN resource_id VARCHAR(1024) NULL AFTER resource_name;
