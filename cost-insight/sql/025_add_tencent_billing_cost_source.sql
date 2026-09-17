-- Keep Tencent disabled until the credential, schema migration, and bounded dry-run pass.
-- Keep source_available_from aligned with COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY.
-- Enable only this source after validation:
-- UPDATE cost_sources SET is_active = 1
-- WHERE vendor = 'tencent' AND account_id = '100050658403';
INSERT INTO cost_sources (
  vendor,
  account_id,
  display_name,
  source_available_from,
  purpose,
  is_active
) VALUES (
  'tencent',
  '100050658403',
  'tencent-organization-billing',
  '2026-09-01',
  'Tencent Cloud organization billing',
  0
)
ON DUPLICATE KEY UPDATE
  display_name = VALUES(display_name),
  source_available_from = VALUES(source_available_from),
  purpose = VALUES(purpose),
  updated_at = CURRENT_TIMESTAMP;
