import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from cost_insight.common.config import TencentBillingSettings
from cost_insight.jobs import cli
import cost_insight.jobs.sync_tencent_billing_summary as job
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_tencent_billing_summary import (
    JOB_NAME,
    SyncTencentBillingSummaryResult,
    _month_coverage_start,
    _same_completion_evidence,
    run_sync_tencent_billing_summary,
)
from cost_insight.sources.tencent_billing import (
    TencentBillMonthSummary,
    TencentBillPage,
    TencentBillSummaryNotReady,
)

ACCOUNT_ID = "100050658403"


def _engine():
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
                  source_table TEXT,
                  source_schema_version TEXT,
                  source_available_from TEXT,
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
        connection.execute(
            text(
                """
                CREATE TABLE cost_bq_export_summary_daily (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  vendor TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  billing_account_id TEXT,
                  export_partition_date TEXT NOT NULL,
                  usage_date TEXT NOT NULL,
                  service_name TEXT,
                  sku_name TEXT,
                  usage_type TEXT,
                  cost_driver_key TEXT,
                  region TEXT,
                  org TEXT,
                  repo TEXT,
                  target_branch TEXT,
                  resource_name TEXT,
                  vendor_tags_json TEXT,
                  author TEXT,
                  source_schema_version TEXT,
                  source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                  cluster_name TEXT,
                  cluster_location TEXT,
                  kubernetes_cost_class TEXT,
                  kubernetes_residual_type TEXT,
                  kubernetes_cost_component TEXT,
                  namespace TEXT,
                  workload_name TEXT,
                  workload_type TEXT,
                  owner TEXT,
                  service TEXT,
                  project TEXT,
                  service_exec_id TEXT,
                  list_cost NUMERIC,
                  effective_cost NUMERIC,
                  credit_amount NUMERIC,
                  net_cost NUMERIC,
                  currency TEXT NOT NULL DEFAULT 'USD',
                  source_export_time TEXT,
                  source_row_hash TEXT NOT NULL,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(vendor, account_id, export_partition_date, source_row_hash)
                )
                """
            )
        )
    return engine


def _detail(
    index: int,
    *,
    day: str = "2026-09-13",
    author: str = "alice",
    org: str = "pingcap-qe",
    repo: str = "cost-insight",
    target_branch: str = "main",
    cost="1.5",
):
    return {
        "BillDay": day,
        "BillId": f"bill-{index}",
        "OrderId": f"order-{index}",
        "ResourceId": f"eks-{index}",
        "FeeBeginTime": f"{day} 01:00:00",
        "FeeEndTime": f"{day} 02:00:00",
        "PayTime": f"{day} 08:00:00",
        "OwnerUin": ACCOUNT_ID,
        "OperateUin": "operator",
        "BusinessCode": "p_eks",
        "ProductCode": "supernode",
        "ActionType": "postpay",
        "RegionId": "ap-beijing",
        "Tags": [
            {"TagKey": "author", "TagValue": author},
            {"TagKey": "org", "TagValue": org},
            {"TagKey": "repo", "TagValue": repo},
            {"TagKey": "target_branch", "TagValue": target_branch},
        ],
        "ComponentSet": [
            {
                "ComponentCode": "cpu",
                "ItemCode": "cpu-time",
                "ComponentConfig": [],
                "Cost": "2.0",
                "RealCost": cost,
            }
        ],
    }


def _page_fetcher(pages, calls):
    def fetch(**kwargs):
        calls.append(dict(kwargs))
        value = pages[(kwargs["bill_day"].isoformat(), kwargs["offset"])]
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


def _range_job_name(day: date) -> str:
    return (
        source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        + f":range:{day.isoformat()}:{day.isoformat()}"
    )


def _copy_range_evidence_to_scheduled_state(engine, day: date, *, last_completed: date) -> None:
    scheduled_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
    with engine.begin() as connection:
        range_watermark = json.loads(
            connection.execute(
                text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                {"name": _range_job_name(day)},
            ).scalar_one()
        )
        scheduled_watermark = {
            "account_id": ACCOUNT_ID,
            "last_completed_bill_day": last_completed.isoformat(),
            "pending_verification": {
                day.isoformat(): range_watermark["pending_verification"][day.isoformat()]
            },
        }
        connection.execute(
            text(
                """
                INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                """
            ),
            {"name": scheduled_name, "watermark": json.dumps(scheduled_watermark)},
        )


def test_tencent_import_commits_pages_checkpoints_and_replays_idempotently() -> None:
    engine = _engine()
    day = date(2026, 9, 13)
    calls = []
    pages = {
        (day.isoformat(), 0): TencentBillPage(
            details=(_detail(1), _detail(2)), total=2, context="page-2"
        ),
        (day.isoformat(), 2): TencentBillPage(details=(), total=None, context=None),
    }
    try:
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=2),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(pages, calls),
            sleep=lambda _seconds: None,
        )
        assert result.bill_days_completed == (day,)
        assert result.outer_rows_seen == 2
        assert result.component_rows_seen == 2
        assert result.touched_usage_dates == (day,)
        assert [call["offset"] for call in calls] == [0, 2]
        assert calls[1]["context"] == "page-2"

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT author, currency, net_cost FROM cost_bq_export_summary_daily "
                    "ORDER BY resource_name"
                )
            ).mappings().all()
            state = connection.execute(
                text(
                    "SELECT watermark_json, last_status FROM cost_job_state WHERE job_name=:name"
                ),
                {"name": _range_job_name(day)},
            ).mappings().one()
        assert len(rows) == 2
        assert {row["currency"] for row in rows} == {"CNY"}
        assert state["last_status"] == "succeeded"
        watermark = json.loads(state["watermark_json"])
        assert watermark["last_completed_bill_day"] == day.isoformat()
        assert "inflight_bill_day" not in watermark
        assert watermark["pending_verification"][day.isoformat()]["outer_row_count"] == 2

        changed_pages = {
            (day.isoformat(), 0): TencentBillPage(
                details=(
                    _detail(
                        1,
                        author="bob",
                        org="new-org",
                        repo="new-repo",
                        target_branch="release-1.0",
                        cost="1.25",
                    ),
                    _detail(
                        2,
                        author="bob",
                        org="new-org",
                        repo="new-repo",
                        target_branch="release-1.0",
                        cost="1.25",
                    ),
                ),
                total=2,
                context=None,
            ),
            (day.isoformat(), 2): TencentBillPage(details=(), total=None, context=None),
        }
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=2),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(changed_pages, []),
            sleep=lambda _seconds: None,
        )
        with engine.connect() as connection:
            replayed = connection.execute(
                text(
                    "SELECT author, org, repo, target_branch, net_cost "
                    "FROM cost_bq_export_summary_daily"
                )
            ).mappings().all()
        assert len(replayed) == 2
        assert {row["author"] for row in replayed} == {"bob"}
        assert {row["org"] for row in replayed} == {"new-org"}
        assert {row["repo"] for row in replayed} == {"new-repo"}
        assert {row["target_branch"] for row in replayed} == {"release-1.0"}
        assert {str(row["net_cost"]) for row in replayed} == {"1.25"}
    finally:
        engine.dispose()


def test_tencent_import_rolls_back_failed_page_and_resumes_offset(monkeypatch) -> None:
    import cost_insight.jobs.sync_tencent_billing_summary as job

    engine = _engine()
    day = date(2026, 9, 13)
    pages = {
        (day.isoformat(), 0): TencentBillPage((_detail(1),), 2, "context-1"),
        (day.isoformat(), 1): TencentBillPage((_detail(2),), None, "context-2"),
        (day.isoformat(), 2): TencentBillPage((), None, None),
    }
    original_write = job._write_summary_rows

    def fail_second_page(connection, rows, **kwargs):
        original_write(connection, rows, **kwargs)
        if rows and rows[0]["resource_name"] == "eks-2":
            raise RuntimeError("database failure")

    monkeypatch.setattr(job, "_write_summary_rows", fail_second_page)
    try:
        with pytest.raises(RuntimeError, match="database failure"):
            run_sync_tencent_billing_summary(
                engine,
                settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=1),
                bill_day_start=day,
                bill_day_end=day,
                fetch_page=_page_fetcher(pages, []),
                sleep=lambda _seconds: None,
            )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 1
            state = connection.execute(
                text(
                    "SELECT watermark_json, last_status FROM cost_job_state WHERE job_name=:name"
                ),
                {"name": _range_job_name(day)},
            ).mappings().one()
        assert state["last_status"] == "failed"
        assert json.loads(state["watermark_json"])["next_offset"] == 1

        monkeypatch.setattr(job, "_write_summary_rows", original_write)
        calls = []
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=1),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(pages, calls),
            sleep=lambda _seconds: None,
        )
        assert result.bill_days_completed == (day,)
        assert [call["offset"] for call in calls] == [1, 2]
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 2
    finally:
        engine.dispose()


def test_tencent_import_normalizes_summary_rows(monkeypatch) -> None:
    engine = _engine()
    day = date(2026, 9, 13)
    normalized = []
    original_normalize = job._normalize_summary_row

    def track_normalization(row, **kwargs):
        normalized.append(row)
        return original_normalize(row, **kwargs)

    monkeypatch.setattr(job, "_normalize_summary_row", track_normalization)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1),), 1, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        assert [row["currency"] for row in normalized] == ["CNY"]
    finally:
        engine.dispose()


def test_tencent_import_retries_expired_context_without_advancing() -> None:
    engine = _engine()
    day = date(2026, 9, 13)
    calls = []

    def fetch(**kwargs):
        calls.append((kwargs["offset"], kwargs["context"]))
        if kwargs["offset"] == 0:
            return TencentBillPage((_detail(1),), 2, "expired-context")
        if kwargs["offset"] == 1 and kwargs["context"]:
            raise RuntimeError("InvalidContext")
        if kwargs["offset"] == 1:
            return TencentBillPage((_detail(2),), None, None)
        return TencentBillPage((), None, None)

    try:
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=1),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=fetch,
            sleep=lambda _seconds: None,
        )
        assert result.outer_rows_seen == 2
        assert calls.count((1, "expired-context")) == 3
        assert (1, None) in calls
    finally:
        engine.dispose()


def test_tencent_dry_run_requires_range_and_does_not_write() -> None:
    engine = _engine()
    day = date(2026, 9, 13)
    pages = {
        (day.isoformat(), 0): TencentBillPage((_detail(1),), 1, None),
    }
    try:
        with pytest.raises(ValueError, match="explicit bill-day range"):
            run_sync_tencent_billing_summary(
                engine,
                settings=TencentBillingSettings(account_id=ACCOUNT_ID),
                dry_run=True,
            )
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=100),
            bill_day_start=day,
            bill_day_end=day,
            dry_run=True,
            fetch_page=_page_fetcher(pages, []),
            sleep=lambda _seconds: None,
        )
        assert result.outer_rows_seen == 1
        assert result.rows_written == 0
        empty_result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, page_size=100),
            bill_day_start=day,
            bill_day_end=day,
            dry_run=True,
            fetch_page=lambda **_kwargs: TencentBillPage((), 0, None),
            sleep=lambda _seconds: None,
        )
        assert empty_result.outer_rows_seen == 0
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM cost_job_state")).scalar_one() == 0
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_explicit_empty_bill_day_completes_without_cost_facts() -> None:
    engine = _engine()
    day = date(2026, 9, 13)
    try:
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=lambda **_kwargs: TencentBillPage((), 0, None),
            sleep=lambda _seconds: None,
        )

        assert result.bill_days_completed == (day,)
        assert result.component_rows_seen == 0
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 0
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": _range_job_name(day)},
                ).scalar_one()
            )
        assert watermark["first_completed_bill_day"] == day.isoformat()
    finally:
        engine.dispose()


def test_scheduled_run_verifies_pending_day_with_single_count_probe() -> None:
    engine = _engine()
    pending_day = date(2026, 9, 11)
    completed_day = date(2026, 9, 13)
    job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
    watermark = {
        "account_id": ACCOUNT_ID,
        "last_completed_bill_day": completed_day.isoformat(),
        "pending_verification": {
            pending_day.isoformat(): {
                "outer_row_count": 2,
                "component_row_count": 2,
            }
        },
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cost_job_state (
                  job_name, watermark_json, last_status, updated_at
                ) VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                """
            ),
            {"name": job_name, "watermark": json.dumps(watermark)},
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_sources (vendor, account_id, display_name, is_active)
                VALUES ('tencent', :account_id, :account_id, 1)
                """
            ),
            {"account_id": ACCOUNT_ID},
        )

    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return TencentBillPage((_detail(1, day=pending_day.isoformat()),), 2, None)

    try:
        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 9, 1),
                page_size=100,
            ),
            now=datetime(2026, 9, 16, 6, tzinfo=UTC),
            fetch_page=probe,
            sleep=lambda _seconds: None,
        )
        assert result.bill_days_completed == ()
        assert result.bill_days_verified == (pending_day,)
        assert len(calls) == 1
        assert calls[0]["limit"] == 1
        assert calls[0]["need_record_num"] is True
        with engine.connect() as connection:
            saved = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert saved["pending_verification"] == {}
        assert saved["last_verification"]["status"] == "verified-count"
    finally:
        engine.dispose()


def test_d5_increased_total_replays_day_pagewise() -> None:
    engine = _engine()
    day = date(2026, 9, 11)
    initial_pages = {
        (day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()),), 1, None)
    }
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(initial_pages, []),
            sleep=lambda _seconds: None,
        )
        _copy_range_evidence_to_scheduled_state(
            engine, day, last_completed=date(2026, 9, 20)
        )
        calls = []

        def fetch(**kwargs):
            calls.append(kwargs)
            details = (
                _detail(1, day=day.isoformat()),
                _detail(2, day=day.isoformat()),
            )
            if kwargs["limit"] == 1:
                return TencentBillPage((details[0],), 2, None)
            return TencentBillPage(details, 2, None)

        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID, earliest_bill_day=date(2026, 9, 1)
            ),
            now=datetime(2026, 9, 16, 6, tzinfo=UTC),
            fetch_page=fetch,
            sleep=lambda _seconds: None,
        )
        assert result.bill_days_verified == (day,)
        assert result.outer_rows_seen == 2
        assert [call["limit"] for call in calls] == [1, 100]
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_bq_export_summary_daily")
            ).scalar_one() == 2
    finally:
        engine.dispose()


def test_d5_persistently_smaller_total_uses_read_only_confirmation() -> None:
    engine = _engine()
    day = date(2026, 9, 11)
    details = (
        _detail(1, day=day.isoformat()),
        _detail(2, day=day.isoformat()),
    )
    initial_pages = {(day.isoformat(), 0): TencentBillPage(details, 2, None)}
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(initial_pages, []),
            sleep=lambda _seconds: None,
        )
        _copy_range_evidence_to_scheduled_state(
            engine, day, last_completed=date(2026, 9, 20)
        )

        def fetch(**kwargs):
            if kwargs["limit"] == 1:
                return TencentBillPage((details[0],), 1, None)
            return TencentBillPage(details, 1, None)

        first = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID, earliest_bill_day=date(2026, 9, 1)
            ),
            now=datetime(2026, 9, 16, 6, tzinfo=UTC),
            fetch_page=fetch,
            sleep=lambda _seconds: None,
        )
        assert first.bill_days_verified == ()

        second = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID, earliest_bill_day=date(2026, 9, 1)
            ),
            now=datetime(2026, 9, 17, 7, tzinfo=UTC),
            fetch_page=fetch,
            sleep=lambda _seconds: None,
        )
        assert second.bill_days_verified == (day,)
        scheduled_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": scheduled_name},
                ).scalar_one()
            )
        assert watermark["last_verification"]["status"] == "verified-stale-total"
    finally:
        engine.dispose()


def test_month_coverage_uses_the_current_month_partition() -> None:
    scheduled_start = date(2026, 9, 1)

    assert _month_coverage_start(
        date(2026, 8, 1),
        scheduled_coverage_start=scheduled_start,
        first_export_partition_date=date(2026, 8, 1),
        completed_range_covers_month=False,
    ) == date(2026, 8, 1)
    assert _month_coverage_start(
        date(2026, 10, 1),
        scheduled_coverage_start=scheduled_start,
        first_export_partition_date=None,
        completed_range_covers_month=False,
    ) == scheduled_start


def test_completion_evidence_tolerates_summary_column_precision() -> None:
    evidence = {
        "outer_row_count": 1,
        "component_row_count": 1,
        "identity_fingerprint": "hash",
        "latest_pay_time": "2026-09-14T08:20:52",
        "list_cost_cny": "1.000000000",
        "effective_cost_cny": "2.000000000",
        "net_cost_cny": "3.000000000",
    }
    rounded = {**evidence, "list_cost_cny": "1.0000000004"}

    assert _same_completion_evidence(evidence, rounded)
    assert not _same_completion_evidence(
        evidence,
        {**evidence, "list_cost_cny": "1.0000000011"},
    )


@pytest.mark.parametrize(
    ("source_total", "expected_source_total"),
    [
        (Decimal("4.0"), "4"),
        # Detail ComponentSet.Cost need not equal the organization summary TotalCost.
        (Decimal("9.99"), "9.99"),
        (None, None),
    ],
)
def test_scheduled_run_reconciles_closed_month_real_cost_with_single_summary_request(
    source_total,
    expected_source_total: str | None,
) -> None:
    engine = _engine()
    day = date(2026, 8, 15)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()), _detail(2, day=day.isoformat())), 2, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": job_name,
                    "watermark": json.dumps(
                        {
                            "account_id": ACCOUNT_ID,
                            "first_completed_bill_day": "2026-08-01",
                            "last_completed_bill_day": "2026-09-07",
                        }
                    ),
                },
            )

        months = []

        def fetch_summary(**kwargs):
            months.append(kwargs["month"])
            return TencentBillMonthSummary(
                month=kwargs["month"],
                total_cost=source_total,
                real_total_cost=Decimal("3.0"),
            )

        result = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 10, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert result.bill_days_completed == ()
        assert months == ["2026-08"]
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        month_evidence = watermark["reconciled_months"]["2026-08"]
        assert month_evidence["status"] == "matched-real-cost-only"
        assert month_evidence["source_total_cost"] == expected_source_total
        assert month_evidence["imported_list_cost"] == "4"
        assert month_evidence["imported_net_cost"] == "3"
    finally:
        engine.dispose()


def test_month_close_mismatch_raises_once_and_skips_refetch_on_next_run() -> None:
    engine = _engine()
    day = date(2026, 8, 15)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()),), 1, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": job_name,
                    "watermark": json.dumps(
                        {
                            "account_id": ACCOUNT_ID,
                            "first_completed_bill_day": "2026-08-01",
                            "last_completed_bill_day": "2026-09-08",
                        }
                    ),
                },
            )

        calls = []
        not_ready = False

        def fetch_summary(**kwargs):
            calls.append(kwargs["month"])
            if not_ready:
                raise TencentBillSummaryNotReady("initializing")
            return TencentBillMonthSummary(
                month=kwargs["month"],
                total_cost=Decimal("9.99"),
                real_total_cost=Decimal("9.99"),
            )

        with pytest.raises(ValueError, match="month-close reconciliation failed"):
            run_sync_tencent_billing_summary(
                engine,
                settings=TencentBillingSettings(
                    account_id=ACCOUNT_ID,
                    earliest_bill_day=date(2026, 8, 1),
                ),
                now=datetime(2026, 9, 10, 6, tzinfo=UTC),
                fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
                fetch_month_summary=fetch_summary,
                sleep=lambda _seconds: None,
            )
        assert calls == ["2026-08"]
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"]["status"] == "mismatch"

        # An unchanged mismatch is not re-detected, so the next schedule keeps flowing.
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 11, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert calls == ["2026-08"]

        # ComponentSet.Cost is observational at month close: changing it alone
        # must not re-open a previously recorded net-cost mismatch.
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET list_cost = 8, effective_cost = 8
                    WHERE vendor = 'tencent' AND account_id = :account_id
                    """
                ),
                {"account_id": ACCOUNT_ID},
            )
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 11, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert calls == ["2026-08"]

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET list_cost = 8, effective_cost = 8, net_cost = 8
                    WHERE vendor = 'tencent' AND account_id = :account_id
                    """
                ),
                {"account_id": ACCOUNT_ID},
            )
        not_ready = True
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 11, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"]["status"] == "mismatch"
        assert watermark["reconciled_months"]["2026-08"]["source_total_cost"] == "9.99"
        assert watermark["reconciled_months"]["2026-08"]["summary_ready"] is False

        not_ready = False
        with pytest.raises(ValueError, match="month-close reconciliation failed"):
            run_sync_tencent_billing_summary(
                engine,
                settings=TencentBillingSettings(
                    account_id=ACCOUNT_ID,
                    earliest_bill_day=date(2026, 8, 1),
                ),
                now=datetime(2026, 9, 11, 6, tzinfo=UTC),
                fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
                fetch_month_summary=fetch_summary,
                sleep=lambda _seconds: None,
            )
        assert calls == ["2026-08", "2026-08", "2026-08"]

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE cost_bq_export_summary_daily
                    SET list_cost = 9.99, effective_cost = 9.99, net_cost = 9.99
                    WHERE vendor = 'tencent' AND account_id = :account_id
                    """
                ),
                {"account_id": ACCOUNT_ID},
            )
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 11, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert calls == ["2026-08", "2026-08", "2026-08", "2026-08"]
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"]["status"] == "matched-real-cost-only"
    finally:
        engine.dispose()


def test_month_close_unready_summary_is_nonfatal_and_retried() -> None:
    engine = _engine()
    day = date(2026, 8, 15)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()),), 1, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": job_name,
                    "watermark": json.dumps(
                        {
                            "account_id": ACCOUNT_ID,
                            "first_completed_bill_day": "2026-08-01",
                            "last_completed_bill_day": "2026-09-08",
                        }
                    ),
                },
            )

        calls = []

        def fetch_summary(**kwargs):
            calls.append(kwargs["month"])
            if len(calls) == 1:
                raise TencentBillSummaryNotReady("initializing")
            return TencentBillMonthSummary(kwargs["month"], Decimal("2"), Decimal("1.5"))

        first = run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, earliest_bill_day=date(2026, 8, 1)),
            now=datetime(2026, 9, 10, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert first.bill_days_completed == ()
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"]["status"] == "unready"

        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, earliest_bill_day=date(2026, 8, 1)),
            now=datetime(2026, 9, 11, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert calls == ["2026-08", "2026-08"]
    finally:
        engine.dispose()


def test_month_close_rechecks_partial_month_after_completed_range_backfill() -> None:
    engine = _engine()
    day = date(2026, 8, 15)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()),), 1, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": job_name,
                    "watermark": json.dumps(
                        {
                            "account_id": ACCOUNT_ID,
                            "first_completed_bill_day": day.isoformat(),
                            "last_completed_bill_day": "2026-09-08",
                        }
                    ),
                },
            )

        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, earliest_bill_day=day),
            now=datetime(2026, 9, 10, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=lambda **kwargs: pytest.fail("no summary fetch expected"),
            sleep=lambda _seconds: None,
        )
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"] == {
            "status": "partial-coverage",
            "coverage_start": day.isoformat(),
        }

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": f"{job_name}:range:2026-08-01:2026-08-31",
                    "watermark": json.dumps(
                        {
                            "account_id": ACCOUNT_ID,
                            "range_start": "2026-08-01",
                            "range_end": "2026-08-31",
                            "last_completed_bill_day": "2026-08-31",
                        }
                    ),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, '{not-json}', 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {"name": f"{job_name}:range:malformed"},
            )
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID, earliest_bill_day=day),
            now=datetime(2026, 9, 10, 6, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=lambda **_kwargs: TencentBillMonthSummary(
                "2026-08", Decimal("2"), Decimal("1.5")
            ),
            sleep=lambda _seconds: None,
        )
        with engine.connect() as connection:
            watermark = json.loads(
                connection.execute(
                    text("SELECT watermark_json FROM cost_job_state WHERE job_name=:name"),
                    {"name": job_name},
                ).scalar_one()
            )
        assert watermark["reconciled_months"]["2026-08"]["status"] == "matched-real-cost-only"
    finally:
        engine.dispose()


def test_month_close_reconciliation_waits_for_beijing_close_time() -> None:
    engine = _engine()
    day = date(2026, 8, 15)
    try:
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(account_id=ACCOUNT_ID),
            bill_day_start=day,
            bill_day_end=day,
            fetch_page=_page_fetcher(
                {(day.isoformat(), 0): TencentBillPage((_detail(1, day=day.isoformat()),), 1, None)},
                [],
            ),
            sleep=lambda _seconds: None,
        )
        job_name = source_job_name(JOB_NAME, vendor="tencent", account_id=ACCOUNT_ID)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cost_job_state (job_name, watermark_json, last_status, updated_at)
                    VALUES (:name, :watermark, 'succeeded', CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "name": job_name,
                    "watermark": json.dumps(
                        {"account_id": ACCOUNT_ID, "last_completed_bill_day": "2026-09-07"}
                    ),
                },
            )

        months = []

        def fetch_summary(**kwargs):
            months.append(kwargs["month"])
            return TencentBillMonthSummary(kwargs["month"], Decimal(0), Decimal(0))

        # 2026-09-01 10:00 Beijing: the August bill is not closed until 19:00 Beijing.
        run_sync_tencent_billing_summary(
            engine,
            settings=TencentBillingSettings(
                account_id=ACCOUNT_ID,
                earliest_bill_day=date(2026, 8, 1),
            ),
            now=datetime(2026, 9, 1, 2, tzinfo=UTC),
            fetch_page=lambda **kwargs: pytest.fail("no page fetch expected"),
            fetch_month_summary=fetch_summary,
            sleep=lambda _seconds: None,
        )
        assert months == []
    finally:
        engine.dispose()


def test_cli_runs_tencent_dry_run(monkeypatch, capsys) -> None:
    captured = {}

    class Engine:
        def dispose(self):
            pass

    settings = SimpleNamespace(
        tencent_billing=TencentBillingSettings(account_id=ACCOUNT_ID),
        log_level="INFO",
    )

    def fake_sync(_engine, **kwargs):
        captured.update(kwargs)
        day = kwargs["bill_day_start"]
        return SyncTencentBillingSummaryResult(
            account_id=ACCOUNT_ID,
            bill_days_completed=(day,),
            bill_days_verified=(),
            outer_rows_seen=1,
            component_rows_seen=1,
            rows_written=0,
            touched_usage_dates=(day,),
            dry_run=True,
        )

    monkeypatch.setattr(cli, "get_settings", lambda require_database=True: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(cli, "run_sync_tencent_billing_summary", fake_sync)

    assert (
        cli.main(
            [
                "sync-tencent-billing-summary",
                "--bill-day-start",
                "2026-09-13",
                "--bill-day-end",
                "2026-09-13",
                "--dry-run",
            ]
        )
        == 0
    )
    assert captured["dry_run"] is True
    assert '"bill_days_completed"' in capsys.readouterr().out
