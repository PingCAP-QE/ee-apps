CREATE TABLE IF NOT EXISTS ci_job_state (
  job_name VARCHAR(64) NOT NULL,
  watermark_json JSON NOT NULL,
  last_started_at DATETIME NULL,
  last_succeeded_at DATETIME NULL,
  last_status VARCHAR(16) NOT NULL DEFAULT 'never',
  last_error TEXT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (job_name)
);
