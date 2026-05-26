CREATE TABLE IF NOT EXISTS ci_l1_flaky_issue_pr_links (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  issue_repo VARCHAR(255) NOT NULL,
  issue_number BIGINT NOT NULL,
  pr_repo VARCHAR(255) NOT NULL,
  pr_number BIGINT NOT NULL,
  pr_url VARCHAR(1024) NOT NULL,
  pr_title VARCHAR(512) NOT NULL,
  link_type VARCHAR(32) NOT NULL,
  source_event_type VARCHAR(32) NOT NULL,
  source_event_id BIGINT NULL,
  linked_at DATETIME NOT NULL,
  source_ticket_updated_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_ci_l1_flaky_issue_pr_links_issue_pr (
    issue_repo,
    issue_number,
    pr_repo,
    pr_number
  ),
  KEY idx_ci_l1_flaky_issue_pr_links_issue (issue_repo, issue_number, linked_at),
  KEY idx_ci_l1_flaky_issue_pr_links_pr (pr_repo, pr_number, linked_at)
);
