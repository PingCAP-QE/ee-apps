ALTER TABLE ci_l1_pod_lifecycle
  DROP COLUMN IF EXISTS jenkins_label_digest;

ALTER TABLE ci_l1_pod_lifecycle
  DROP COLUMN IF EXISTS jenkins_controller;
