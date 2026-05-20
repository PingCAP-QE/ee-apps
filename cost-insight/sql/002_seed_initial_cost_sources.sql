INSERT INTO cost_sources (
  vendor,
  account_id,
  billing_account_id,
  display_name,
  is_active
) VALUES (
  'gcp',
  'pingcap-testing-account',
  '01D088-8F9CF2-8AF1C6',
  'pingcap-testing-account',
  1
)
ON DUPLICATE KEY UPDATE
  billing_account_id = VALUES(billing_account_id),
  display_name = VALUES(display_name),
  is_active = VALUES(is_active),
  updated_at = CURRENT_TIMESTAMP;
