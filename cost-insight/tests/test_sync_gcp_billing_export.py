from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import GcpBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.sync_gcp_billing_export import (
    JOB_NAME,
    _coerce_date,
    _coerce_datetime,
    _ensure_cost_source_enabled,
    _hash_value,
    _normalize_row,
    _nullable_text,
    _start_date_from_state,
    _watermark,
    _write_batch,
    build_source_row_hash,
    run_sync_gcp_billing_export,
)


def test_source_row_hash_ignores_amount_changes() -> None:
    row = {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "billing_account_id": "billing-1",
        "usage_date": date(2026, 5, 18),
        "service_name": "Compute Engine",
        "sku_name": "C4 Instance Core running in Americas",
        "region": "us-central1",
        "namespace": "prow-test-pods",
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "ticdc",
        "resource_name": "cap-ticdc-pull-123",
        "net_cost": Decimal("1.23"),
    }
    changed_amount = {**row, "net_cost": Decimal("2.34")}

    assert build_source_row_hash(row) == build_source_row_hash(changed_amount)


def test_normalize_row_adds_dimension_hash() -> None:
    row = _normalize_row(
        {
            "vendor": "gcp",
            "account_id": "pingcap-testing-account",
            "billing_account_id": "billing-1",
            "usage_date": "2026-05-18",
            "service_name": "Compute Engine",
            "sku_name": "C4 Instance Core running in Americas",
            "region": "us-central1",
            "namespace": "prow-test-pods",
            "author": "hawkingrei",
            "org": "pingcap",
            "repo": "ticdc",
            "resource_name": "cap-ticdc-pull-123",
            "usage_seconds": "3600.00",
            "list_cost": "1.00",
            "effective_cost": "0.80",
            "credit_amount": "-0.20",
            "net_cost": "0.60",
            "source_export_time": "2026-05-18T12:00:00Z",
        }
    )

    assert row["usage_date"] == date(2026, 5, 18)
    assert row["source_export_time"] == datetime(2026, 5, 18, 12, 0, 0)
    assert row["net_cost"] == Decimal("0.60")
    assert len(row["source_row_hash"]) == 64


def test_normalize_row_rejects_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="Missing account_id"):
        _normalize_row({"usage_date": "2026-05-18"})

    with pytest.raises(ValueError, match="Missing usage_date"):
        _normalize_row({"account_id": "pingcap-testing-account"})


def test_coercion_helpers() -> None:
    assert _nullable_text("  value  ") == "value"
    assert _nullable_text("   ") is None
    assert _coerce_date(datetime(2026, 5, 18, 12, 0)) == date(2026, 5, 18)
    assert _coerce_date(date(2026, 5, 18)) == date(2026, 5, 18)
    assert _coerce_datetime(datetime(2026, 5, 18, 12, 0)).tzinfo is None
    assert _coerce_datetime("2026-05-18T12:00:00+08:00") == datetime(2026, 5, 18, 4, 0)
    assert _coerce_datetime(datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)) == datetime(
        2026, 5, 18, 12, 0
    )
    assert _hash_value(Decimal("1.20")) == "1.20"


def test_coercion_helpers_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="Unsupported date value"):
        _coerce_date(123)

    with pytest.raises(ValueError, match="Unsupported datetime value"):
        _coerce_datetime(123)


def test_start_date_from_state_uses_overlap() -> None:
    assert _start_date_from_state({}, end_date=date(2026, 5, 18), overlap_days=3) == date(
        2026, 5, 18
    )
    assert _start_date_from_state(
        {"end_date": "2026-05-18"},
        end_date=date(2026, 5, 20),
        overlap_days=3,
    ) == date(2026, 5, 15)
    assert _start_date_from_state(
        {"end_date": "2026-05-25"},
        end_date=date(2026, 5, 20),
        overlap_days=3,
    ) == date(2026, 5, 20)


def test_watermark_formats_dates() -> None:
    assert _watermark(
        account_id="pingcap-testing-account",
        start_date=date(2026, 5, 17),
        end_date=date(2026, 5, 18),
    ) == {
        "account_id": "pingcap-testing-account",
        "start_date": "2026-05-17",
        "end_date": "2026-05-18",
    }


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
        connection.execute(
            text(
                """
                CREATE TABLE cost_job_state (
                  job_name TEXT PRIMARY KEY,
                  watermark_json TEXT,
                  last_started_at TEXT,
                  last_succeeded_at TEXT,
                  last_status TEXT,
                  last_error TEXT,
                  updated_at TEXT
                )
                """
            )
        )
    return engine


def _billing_row(day: str = "2026-05-18") -> dict[str, object]:
    return {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "billing_account_id": "billing-1",
        "usage_date": day,
        "service_name": "Compute Engine",
        "sku_name": "C4 Instance Core running in Americas",
        "region": "us-central1",
        "namespace": "prow-test-pods",
        "author": "hawkingrei",
        "org": "pingcap",
        "repo": "ticdc",
        "resource_name": "cap-ticdc-pull-123",
        "usage_seconds": "3600.00",
        "list_cost": "1.00",
        "effective_cost": "0.80",
        "credit_amount": "-0.20",
        "net_cost": "0.60",
        "source_export_time": "2026-05-18T12:00:00Z",
    }


def test_run_sync_gcp_billing_export_dry_run_does_not_update_state() -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=2)

    try:
        summary = run_sync_gcp_billing_export(
            engine,
            settings=settings,
            start_date=date(2026, 5, 18),
            end_date=date(2026, 5, 18),
            dry_run=True,
            fetch_rows=lambda **_kwargs: [_billing_row()],
        )

        assert summary.rows_seen == 1
        assert summary.rows_written == 0
        assert summary.dry_run is True
        with engine.begin() as connection:
            assert state_store.get_job_state(connection, JOB_NAME) is None
            source = connection.execute(text("SELECT * FROM cost_sources")).mappings().first()
        assert source is None
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_export_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account", page_size=1)
    writes = []

    def fake_write_batch(_engine, rows, *, dry_run):
        writes.append((list(rows), dry_run))
        return len(rows)

    monkeypatch.setattr(
        "cost_insight.jobs.sync_gcp_billing_export._write_batch",
        fake_write_batch,
    )

    try:
        summary = run_sync_gcp_billing_export(
            engine,
            settings=settings,
            start_date=date(2026, 5, 18),
            end_date=date(2026, 5, 18),
            fetch_rows=lambda **_kwargs: [_billing_row(), _billing_row()],
        )

        assert summary.rows_seen == 2
        assert summary.rows_written == 2
        assert [len(rows) for rows, _dry_run in writes] == [1, 1, 0]
        with engine.begin() as connection:
            state = state_store.get_job_state(connection, JOB_NAME)
            source = (
                connection.execute(
                    text(
                        """
                        SELECT vendor, account_id, billing_account_id, display_name, is_active
                        FROM cost_sources
                        """
                    )
                )
                .mappings()
                .one()
            )
        assert state is not None
        assert state.last_status == "succeeded"
        assert dict(source) == {
            "vendor": "gcp",
            "account_id": "pingcap-testing-account",
            "billing_account_id": "billing-1",
            "display_name": "pingcap-testing-account",
            "is_active": 1,
        }
    finally:
        engine.dispose()


def test_run_sync_gcp_billing_export_marks_failure(monkeypatch) -> None:
    engine = _sqlite_engine()
    settings = GcpBillingSettings(account_id="pingcap-testing-account")

    def raise_fetch(**_kwargs):
        raise RuntimeError("boom")
        yield

    try:
        with pytest.raises(RuntimeError, match="boom"):
            run_sync_gcp_billing_export(
                engine,
                settings=settings,
                start_date=date(2026, 5, 18),
                end_date=date(2026, 5, 18),
                fetch_rows=raise_fetch,
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(connection, JOB_NAME)
        assert state is not None
        assert state.last_status == "failed"
        assert "RuntimeError" in (state.last_error or "")
    finally:
        engine.dispose()


def test_write_batch_executes_upsert_for_non_dry_run() -> None:
    class Connection:
        def __init__(self):
            self.executed = []

        def execute(self, statement, rows):
            self.executed.append((statement, rows))

    class Begin:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, *_args):
            return None

    class Engine:
        def __init__(self):
            self.connection = Connection()

        def begin(self):
            return Begin(self.connection)

    engine = Engine()
    row = _normalize_row(_billing_row())

    assert _write_batch(engine, [], dry_run=False) == 0
    assert _write_batch(engine, [row], dry_run=True) == 0
    assert _write_batch(engine, [row], dry_run=False) == 1
    assert len(engine.connection.executed) == 1


def test_ensure_cost_source_enabled_rejects_inactive_source() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_sources (vendor, account_id, is_active)
                    VALUES ('gcp', 'pingcap-testing-account', 0)
                    """
                )
            )
            with pytest.raises(ValueError, match="inactive"):
                _ensure_cost_source_enabled(
                    connection,
                    GcpBillingSettings(account_id="pingcap-testing-account"),
                    dry_run=False,
                )
    finally:
        engine.dispose()
