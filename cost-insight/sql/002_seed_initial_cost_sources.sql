INSERT INTO cost_sources (
  vendor,
  account_id,
  billing_account_id,
  display_name,
  purpose,
  is_active
) VALUES
(
  'gcp',
  'pingcap-testing-account',
  '01D088-8F9CF2-8AF1C6',
  'pingcap-testing-account',
  NULL,
  1
),
(
  'gcp',
  'qa-infra-dev',
  '01D088-8F9CF2-8AF1C6',
  'qa-infra-dev',
  '机器统一资源池',
  1
),
(
  'aws',
  '946646677266',
  '946646677266',
  'qa-infra-dev',
  '机器统一资源池及重点项目测试',
  1
)
ON DUPLICATE KEY UPDATE
  billing_account_id = VALUES(billing_account_id),
  display_name = VALUES(display_name),
  purpose = COALESCE(VALUES(purpose), purpose),
  is_active = VALUES(is_active),
  updated_at = CURRENT_TIMESTAMP;
