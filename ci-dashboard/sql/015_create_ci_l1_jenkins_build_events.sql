CREATE TABLE IF NOT EXISTS ci_l1_jenkins_build_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  event_id VARCHAR(128) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  event_time DATETIME NULL,
  received_at DATETIME NOT NULL,
  normalized_build_url VARCHAR(1024) NULL,
  build_url VARCHAR(1024) NULL,
  result VARCHAR(32) NULL,
  payload_json JSON NOT NULL,
  processing_status VARCHAR(32) NOT NULL DEFAULT 'RECEIVED',
  last_error TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ci_l1_jenkins_build_events_event_id (event_id),
  KEY idx_ci_l1_jenkins_build_events_build_url (normalized_build_url(768))
);
