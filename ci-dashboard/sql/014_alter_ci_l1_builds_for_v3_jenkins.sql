ALTER TABLE ci_l1_builds
  MODIFY COLUMN source_prow_row_id BIGINT NULL,
  MODIFY COLUMN source_prow_job_id CHAR(36) NULL,
  MODIFY COLUMN namespace VARCHAR(255) NULL,
  MODIFY COLUMN job_name VARCHAR(255) NULL,
  MODIFY COLUMN job_type VARCHAR(32) NULL,
  MODIFY COLUMN org VARCHAR(63) NULL,
  MODIFY COLUMN repo VARCHAR(63) NULL,
  MODIFY COLUMN repo_full_name VARCHAR(127) NULL,
  MODIFY COLUMN start_time DATETIME NULL;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS source_jenkins_event_id VARCHAR(128) NULL AFTER build_system;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS source_jenkins_job_url VARCHAR(1024) NULL AFTER source_jenkins_event_id;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS source_jenkins_result VARCHAR(32) NULL AFTER source_jenkins_job_url;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS build_params_json JSON NULL AFTER source_jenkins_result;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS log_gcs_uri VARCHAR(512) NULL AFTER build_params_json;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS log_archived_at DATETIME NULL AFTER log_gcs_uri;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_error_l1_category VARCHAR(32) NULL AFTER log_archived_at;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_error_l2_subcategory VARCHAR(64) NULL AFTER ai_error_l1_category;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_classification_source VARCHAR(32) NULL AFTER ai_error_l2_subcategory;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_classification_confidence DECIMAL(4,3) NULL AFTER ai_classification_source;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_classified_at DATETIME NULL AFTER ai_classification_confidence;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_provider_name VARCHAR(64) NULL AFTER ai_classified_at;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_model_name VARCHAR(64) NULL AFTER ai_provider_name;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS ai_evidence_text TEXT NULL AFTER ai_model_name;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS human_error_l1_category VARCHAR(32) NULL AFTER ai_evidence_text;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS human_error_l2_subcategory VARCHAR(64) NULL AFTER human_error_l1_category;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS human_reviewed_at DATETIME NULL AFTER human_error_l2_subcategory;

ALTER TABLE ci_l1_builds
  ADD COLUMN IF NOT EXISTS human_reviewer VARCHAR(128) NULL AFTER human_reviewed_at;
