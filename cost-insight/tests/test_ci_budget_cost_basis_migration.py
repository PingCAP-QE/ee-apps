from pathlib import Path


SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def test_ci_budget_cost_basis_migration_requires_one_tencent_plan_before_seeding() -> None:
    migration = (SQL_DIR / "029_add_ci_budget_cost_basis.sql").read_text(encoding="utf-8")
    normalized = "\n".join(line.strip() for line in migration.splitlines())
    target_predicate = """WHERE vendor = 'tencent'
AND LOWER(TRIM(platform)) = 'cicd'
AND JSON_CONTAINS(accounts, JSON_QUOTE('100050658403'))
AND budget_name = 'PingCAP CICD H2 Tencent 2026'
AND period_start_date = '2026-09-01'
AND period_end_date = '2027-03-31'"""
    sentinel = f"""SELECT CASE
WHEN COUNT(*) = 1 THEN 'CI_BUDGET_TARGET_COUNT_OK'
ELSE JSON_EXTRACT(CONCAT('CI_BUDGET_TARGET_COUNT_INVALID:', COUNT(*)), '$')
END AS seed_guard
FROM cost_budgets
{target_predicate};"""
    guarded_update = f"""{target_predicate}
AND 1 = (
SELECT COUNT(*)
FROM (
SELECT id
FROM cost_budgets
{target_predicate}
GROUP BY id
) AS ci_budget_target
);"""

    assert "ADD COLUMN IF NOT EXISTS cost_basis VARCHAR(16) NOT NULL DEFAULT 'list_cost'" in migration
    assert sentinel in normalized
    assert normalized.count(target_predicate) == 3
    assert guarded_update in normalized
    assert "SET cost_basis = 'net_cost'" in migration
    assert "account_id" not in migration[migration.index("SELECT CASE") :]
