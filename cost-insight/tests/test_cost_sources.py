from types import SimpleNamespace

from sqlalchemy import create_engine, text

from cost_insight.jobs.cost_sources import (
    _build_upsert_cost_source_statement,
    ensure_cost_source_enabled,
    get_cost_source,
    list_active_cost_sources,
    upsert_cost_source,
)


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_sources (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  vendor TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  billing_account_id TEXT,
                  display_name TEXT,
                  is_active INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(vendor, account_id)
                )
                """
            )
        )
    return engine


def test_upsert_and_list_active_cost_sources() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            upsert_cost_source(
                connection,
                vendor="gcp",
                account_id="qa-infra-dev",
                billing_account_id="01D088-8F9CF2-8AF1C6",
                display_name="QA Infra Dev",
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_sources (vendor, account_id, display_name, is_active)
                    VALUES ('aws', 'inactive-account', 'Inactive', 0)
                    """
                )
            )

            source = get_cost_source(connection, vendor="gcp", account_id="qa-infra-dev")
            active_sources = list_active_cost_sources(connection)
            active_gcp_sources = list_active_cost_sources(connection, vendor="gcp")

        assert source is not None
        assert source.billing_account_id == "01D088-8F9CF2-8AF1C6"
        assert [(item.vendor, item.account_id) for item in active_sources] == [
            ("gcp", "qa-infra-dev"),
        ]
        assert [(item.vendor, item.account_id) for item in active_gcp_sources] == [
            ("gcp", "qa-infra-dev"),
        ]
    finally:
        engine.dispose()


def test_ensure_cost_source_enabled_inserts_only_when_not_dry_run() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            ensure_cost_source_enabled(
                connection,
                vendor="aws",
                account_id="946646677266",
                dry_run=True,
                display_name="AWS Dry Run",
            )
            assert get_cost_source(connection, vendor="aws", account_id="946646677266") is None

        with engine.begin() as connection:
            ensure_cost_source_enabled(
                connection,
                vendor="aws",
                account_id="946646677266",
                dry_run=False,
                display_name="AWS Active",
            )
            source = get_cost_source(connection, vendor="aws", account_id="946646677266")

        assert source is not None
        assert source.display_name == "AWS Active"
    finally:
        engine.dispose()


def test_ensure_cost_source_enabled_rejects_inactive_source() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_sources (vendor, account_id, display_name, is_active)
                    VALUES ('aws', '946646677266', 'AWS Inactive', 0)
                    """
                )
            )
            try:
                ensure_cost_source_enabled(
                    connection,
                    vendor="aws",
                    account_id="946646677266",
                    dry_run=False,
                )
            except ValueError as exc:
                assert "inactive" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected ValueError")
    finally:
        engine.dispose()


def test_build_upsert_cost_source_statement_supports_mysql() -> None:
    connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    statement = _build_upsert_cost_source_statement(connection)

    assert "ON DUPLICATE KEY UPDATE" in str(statement)
