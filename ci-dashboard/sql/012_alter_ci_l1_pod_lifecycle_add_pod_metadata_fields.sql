ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_labels_json TEXT NULL AFTER build_system;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_annotations_json TEXT NULL AFTER pod_labels_json;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS metadata_observed_at DATETIME NULL AFTER pod_annotations_json;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_author VARCHAR(255) NULL AFTER metadata_observed_at;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_org VARCHAR(255) NULL AFTER pod_author;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS pod_repo VARCHAR(255) NULL AFTER pod_org;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS jenkins_label VARCHAR(255) NULL AFTER pod_repo;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS jenkins_label_digest VARCHAR(255) NULL AFTER jenkins_label;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS jenkins_controller VARCHAR(255) NULL AFTER jenkins_label_digest;

ALTER TABLE ci_l1_pod_lifecycle
  ADD COLUMN IF NOT EXISTS ci_job VARCHAR(255) NULL AFTER jenkins_controller;
