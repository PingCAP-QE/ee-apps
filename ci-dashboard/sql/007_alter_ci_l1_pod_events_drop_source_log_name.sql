ALTER TABLE ci_l1_pod_events
  DROP INDEX IF EXISTS uq_ci_l1_pod_events_source;

ALTER TABLE ci_l1_pod_events
  MODIFY COLUMN source_insert_id VARCHAR(255) NOT NULL AFTER reporting_instance;

ALTER TABLE ci_l1_pod_events
  DROP COLUMN IF EXISTS source_log_name;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ci_l1_pod_events_source
  ON ci_l1_pod_events (source_project, source_insert_id);
