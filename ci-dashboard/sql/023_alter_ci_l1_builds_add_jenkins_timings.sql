ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS jenkins_blocked_subtasks_sum INT NULL AFTER build_system;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS jenkins_buildable_subtasks_sum INT NULL AFTER jenkins_blocked_subtasks_sum;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS jenkins_queue_total_subtasks_sum INT NULL AFTER jenkins_buildable_subtasks_sum;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS jenkins_building_subtasks_sum INT NULL AFTER jenkins_queue_total_subtasks_sum;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS jenkins_subtask_count INT NULL AFTER jenkins_building_subtasks_sum;
