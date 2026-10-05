INSERT INTO cost_sources (
  vendor,
  account_id,
  billing_account_id,
  display_name,
  purpose,
  source_available_from,
  is_active
) VALUES (
  'aws',
  '131464424160',
  '317766989874',
  'qa-infra-prod',
  'essential v2 canary release PRD env',
  '2026-06-01',
  1
)
ON DUPLICATE KEY UPDATE
  billing_account_id = VALUES(billing_account_id),
  display_name = VALUES(display_name),
  purpose = VALUES(purpose),
  source_available_from = VALUES(source_available_from),
  is_active = VALUES(is_active),
  updated_at = CURRENT_TIMESTAMP;
