ALTER TABLE cost_sources
  ADD COLUMN IF NOT EXISTS account_category VARCHAR(32) NULL AFTER purpose;

UPDATE cost_sources
SET account_category = CASE
  WHEN NULLIF(TRIM(purpose), '') IS NOT NULL THEN 'QA'
  ELSE 'CI'
END
WHERE is_active = 1
  AND NULLIF(TRIM(account_category), '') IS NULL;
