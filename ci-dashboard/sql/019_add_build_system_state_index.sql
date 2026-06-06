CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_build_system_state_start_id
  ON ci_l1_builds (build_system, state, start_time DESC, id DESC);
