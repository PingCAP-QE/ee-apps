INSERT INTO cost_sources (
  vendor,
  account_id,
  billing_account_id,
  display_name,
  purpose,
  source_available_from,
  is_active
) VALUES
(
  'gcp',
  'pingcap-testing-account',
  '01D088-8F9CF2-8AF1C6',
  'pingcap-testing-account',
  NULL,
  NULL,
  1
),
(
  'gcp',
  'qa-infra-dev',
  '01D088-8F9CF2-8AF1C6',
  'qa-infra-dev',
  '机器统一资源池',
  NULL,
  1
),
(
  'aws',
  '946646677266',
  '946646677266',
  'qa-infra-dev',
  '机器统一资源池及重点项目测试',
  NULL,
  1
),
(
  'aws',
  '131464424160',
  '317766989874',
  'qa-infra-prod',
  'essential v2 canary release PRD env',
  '2026-06-01',
  1
),
(
  'azure',
  'aaa5414d-7537-4e24-99bd-a7a841221810',
  NULL,
  'azure-testing-infra-dev',
  NULL,
  NULL,
  1
),
(
  'azure',
  'abd27163-b965-4217-8cba-2a4c799579fe',
  NULL,
  'azure-testing-infra-prod-dataplane',
  NULL,
  NULL,
  1
),
(
  'alibaba',
  '5028760335873601',
  '5028760335873601',
  'alicloud-testing-infra-dev',
  NULL,
  NULL,
  1
)
ON DUPLICATE KEY UPDATE
  billing_account_id = VALUES(billing_account_id),
  display_name = VALUES(display_name),
  purpose = COALESCE(VALUES(purpose), purpose),
  is_active = VALUES(is_active),
  updated_at = CURRENT_TIMESTAMP;
