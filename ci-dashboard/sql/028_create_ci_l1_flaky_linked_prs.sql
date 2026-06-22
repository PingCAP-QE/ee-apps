CREATE TABLE IF NOT EXISTS ci_l1_flaky_linked_prs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  pr_repo VARCHAR(255) NOT NULL,
  pr_number BIGINT NOT NULL,
  pr_url VARCHAR(1024) NOT NULL,
  pr_title VARCHAR(512) NOT NULL,
  pr_state VARCHAR(32) NOT NULL,
  pr_created_at DATETIME NOT NULL,
  pr_closed_at DATETIME NULL,
  pr_merged_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_ci_l1_flaky_linked_prs_repo_pr (pr_repo, pr_number),
  KEY idx_ci_l1_flaky_linked_prs_state_time (pr_state, pr_merged_at)
);
