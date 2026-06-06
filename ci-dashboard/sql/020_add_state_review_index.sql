CREATE INDEX IF NOT EXISTS idx_ci_l1_builds_state_review_time_id
  ON ci_l1_builds (state, revise_error_l1_category, revise_error_l2_subcategory, start_time DESC, id DESC);
