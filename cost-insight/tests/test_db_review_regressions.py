"""Regression checks for the transport resilience change review."""
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from cost_insight.common import db_retry
from cost_insight.common.config import (
    AwsBillingSettings,
    AzureBillingSettings,
    TencentBillingSettings,
)
from cost_insight.jobs import state_store
from cost_insight.jobs import sync_aws_billing_summary as aws
from cost_insight.jobs import sync_azure_billing_summary as azure
from cost_insight.jobs import sync_gcp_billing_summary as summary
from cost_insight.jobs import sync_tencent_billing_summary as tencent
from test_db_resilience import DAY, CommitFaultEngine, disconnect, replace, resource_names, row
from test_sync_aws_jobs import _sqlite_engine


def runner(vendor, engine, monkeypatch, error=None, replacement=False, scoped=False):
    if vendor == "tencent":
        monkeypatch.setattr(tencent, "_import_bill_day", Mock(
            side_effect=error,
            return_value=({"outer_row_count": 0, "component_row_count": 0}, set()),
        ))
        return tencent.run_sync_tencent_billing_summary(
            engine, settings=TencentBillingSettings(account_id="test"),
            bill_day_start=DAY, bill_day_end=DAY, now=datetime(2026, 5, 22, tzinfo=UTC),
        )
    rows = [{
        **row("new-a"), "vendor": vendor, "account_id": "test",
        "export_partition_date": DAY.replace(day=1) if vendor == "azure" else DAY,
    }] if replacement else []
    fetch = Mock(side_effect=error, return_value=rows)
    module, settings = (aws, AwsBillingSettings()) if vendor == "aws" else (azure, AzureBillingSettings())
    return getattr(module, f"run_sync_{vendor}_billing_summary")(
        engine, settings=settings, account_id="test", fetch_rows=fetch,
        export_partition_start=DAY, export_partition_end=DAY,
        replace_existing_partitions=replacement,
        **({
            "replacement_usage_start_date": DAY,
            "replacement_usage_end_date": DAY,
        } if scoped else {}),
    )


@pytest.mark.parametrize("vendor", ["aws", "azure", "tencent"])
def test_vendor_bookkeeping_error_cannot_mask_original(monkeypatch, vendor):
    engine = _sqlite_engine()
    original = ValueError("invalid billing source")
    monkeypatch.setattr(state_store, "mark_job_failed", Mock(side_effect=disconnect()))
    try:
        with pytest.raises(ValueError) as exc:
            runner(vendor, engine, monkeypatch, error=original)
        assert exc.value is original
        state_store.mark_job_failed.assert_called_once()
    finally:
        engine.dispose()


@pytest.mark.parametrize("vendor", ["aws", "azure", "tencent"])
def test_vendor_terminal_success_ack_loss_does_not_flip_state(monkeypatch, vendor):
    engine = _sqlite_engine()
    # Tencent starts/checkpoints an empty day before its terminal success write.
    faulty = CommitFaultEngine(engine, ack_at=3 if vendor == "tencent" else 2)
    try:
        with pytest.raises(OperationalError) as exc:
            runner(vendor, faulty, monkeypatch)
        assert exc.value is faulty.error
        with engine.connect() as conn:
            assert conn.execute(text("SELECT last_status FROM cost_job_state")).scalar_one() == "succeeded"
    finally:
        engine.dispose()


@pytest.mark.parametrize("vendor,scoped", [("aws", False), ("azure", False), ("azure", True)])
@pytest.mark.parametrize("retry", [False, True])
def test_vendor_existing_spool_is_not_copied_again(monkeypatch, vendor, scoped, retry):
    engine = _sqlite_engine()
    monkeypatch.setattr(summary.tempfile, "TemporaryFile", Mock(wraps=summary.tempfile.TemporaryFile))
    monkeypatch.setattr(db_retry.time, "sleep", Mock())
    inserts = []

    def fault(connection, cursor, statement, params, context, many):
        if statement.replace("`", "").lstrip().startswith("INSERT INTO cost_bq_export_summary_daily"):
            inserts.append(statement)
            if retry and len(inserts) == 1:
                raise disconnect()

    event.listen(engine, "before_cursor_execute", fault)
    try:
        result = runner(vendor, engine, monkeypatch, replacement=True, scoped=scoped)
        assert result.rows_seen == result.rows_written == 1
        assert resource_names(engine) == ["new-a"]
        assert len(inserts) == (2 if retry else 1)
        # Replays must rewind the vendor's real spool, not create a second copy.
        assert summary.tempfile.TemporaryFile.call_count == 1
        with engine.connect() as conn:
            assert conn.execute(text("SELECT last_status FROM cost_job_state")).scalar_one() == "succeeded"
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["partition_usage", "partitions", "usage"])
def test_frozen_row_factory_rewinds_without_second_spool(monkeypatch, mode):
    engine = _sqlite_engine()
    monkeypatch.setattr(summary.tempfile, "TemporaryFile", Mock(side_effect=AssertionError("extra spool")))
    source = [row("new-a"), row("new-b")]
    factory = Mock(side_effect=lambda: iter(source))
    faulty = CommitFaultEngine(engine)
    try:
        assert replace(faulty, factory, mode) == 2
        assert factory.call_count == 2
        assert resource_names(engine) == ["new-a", "new-b"]
    finally:
        engine.dispose()
