CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_pod_name_time
  ON ci_l1_builds (pod_name, start_time);

CREATE INDEX IF NOT EXISTS idx_ci_l1_pod_events_identity_time
  ON ci_l1_pod_events (source_project, namespace_name, pod_uid, pod_name, event_timestamp);
