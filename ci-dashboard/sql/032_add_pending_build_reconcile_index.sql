CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_pending_reconcile
  ON ci_l1_builds (state, completion_time, start_time, id);
