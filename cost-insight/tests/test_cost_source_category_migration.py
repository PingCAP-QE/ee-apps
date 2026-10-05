from pathlib import Path


def test_category_migration_backfills_only_active_uncategorized_sources() -> None:
    migration = (
        Path(__file__).resolve().parents[1] / "sql" / "027_add_cost_source_category.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS category VARCHAR(32) NULL" in migration
    assert "WHEN NULLIF(TRIM(purpose), '') IS NOT NULL THEN 'QA'" in migration
    assert "ELSE 'CI'" in migration
    assert "WHERE is_active = 1" in migration
    assert "NULLIF(TRIM(category), '') IS NULL" in migration
    assert "INDEX" not in migration
