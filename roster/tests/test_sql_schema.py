from __future__ import annotations

from pathlib import Path


SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "001_create_roster_tables.sql"
JOIN_TIME_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "002_alter_roster_employees_add_join_time.sql"
)


def test_schema_migration_creates_expected_tables() -> None:
    sql = SQL_PATH.read_text()

    assert "CREATE TABLE IF NOT EXISTS roster_employees" in sql
    assert "CREATE TABLE IF NOT EXISTS roster_groups" in sql
    assert sql.count("ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci") == 2


def test_employee_schema_has_join_keys_and_paths() -> None:
    sql = SQL_PATH.read_text()

    assert "id BIGINT NOT NULL AUTO_INCREMENT" in sql
    assert "lark_id VARCHAR(128) NOT NULL" in sql
    assert "employee_no VARCHAR(64) NULL" in sql
    assert "join_time DATETIME NULL" in sql
    assert "UNIQUE KEY uk_roster_employees_lark_id (lark_id)" in sql
    assert "UNIQUE KEY uk_roster_employees_email (email)" in sql
    assert "UNIQUE KEY uk_roster_employees_github_id (github_id)" in sql
    assert "KEY idx_roster_employees_employee_no (employee_no)" in sql
    assert "manager_path VARCHAR(1024) NULL" in sql
    assert "group_path VARCHAR(1024) NULL" in sql
    assert "last_seen_at DATETIME NULL" in sql


def test_group_schema_has_parent_manager_and_path() -> None:
    sql = SQL_PATH.read_text()

    assert "lark_group_id VARCHAR(128) NOT NULL" in sql
    assert "parent_id BIGINT NULL" in sql
    assert "manager_id BIGINT NULL" in sql
    assert "path VARCHAR(1024) NULL" in sql
    assert "UNIQUE KEY uk_roster_groups_lark_group_id (lark_group_id)" in sql
    assert "KEY idx_roster_groups_parent (parent_id)" in sql
    assert "KEY idx_roster_groups_manager (manager_id)" in sql


def test_join_time_alter_sql_adds_employee_column() -> None:
    sql = JOIN_TIME_SQL_PATH.read_text()

    assert "ALTER TABLE roster_employees" in sql
    assert "ADD COLUMN join_time DATETIME NULL AFTER github_id" in sql
