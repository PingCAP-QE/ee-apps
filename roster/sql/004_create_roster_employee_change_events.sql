CREATE TABLE IF NOT EXISTS roster_employee_change_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  event_type VARCHAR(32) NOT NULL,
  employee_lark_id VARCHAR(128) NOT NULL,
  employee_name VARCHAR(255) NOT NULL,
  employee_email VARCHAR(255) NULL,
  manager_name VARCHAR(255) NULL,
  manager_email VARCHAR(255) NULL,
  group_name VARCHAR(255) NULL,
  group_path VARCHAR(1024) NULL,
  previous_group_name VARCHAR(255) NULL,
  previous_group_path VARCHAR(1024) NULL,
  event_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_roster_employee_change_events_event_at (event_at),
  KEY idx_roster_employee_change_events_type_time (event_type, event_at),
  KEY idx_roster_employee_change_events_employee (employee_lark_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
