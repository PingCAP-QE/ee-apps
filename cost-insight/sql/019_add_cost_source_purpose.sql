ALTER TABLE cost_sources
  ADD COLUMN IF NOT EXISTS purpose VARCHAR(255) NULL AFTER display_name;
