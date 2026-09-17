from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from cost_insight.common.config import TencentBillingSettings
from cost_insight.jobs import state_store
from cost_insight.jobs.cost_sources import ensure_cost_source_enabled
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.sync_gcp_billing_summary import (
    _normalize_summary_row,
    _write_summary_rows,
)
from cost_insight.sources.tencent_billing import (
    TencentBillMonthSummary,
    TencentBillPage,
    TencentBillSummaryNotReady,
    expand_tencent_bill_details,
    fetch_tencent_bill_detail_page,
    fetch_tencent_bill_month_summary,
)

LOG = logging.getLogger(__name__)
JOB_NAME = "sync_tencent_billing_summary"
VENDOR = "tencent"
_BEIJING = ZoneInfo("Asia/Shanghai")
_AMOUNT_QUANTUM = Decimal("0.000000001")
_MATCHED_MONTH_STATUSES = frozenset({"matched", "matched-real-cost-only"})
_INFLIGHT_KEYS = (
    "inflight_bill_day",
    "next_offset",
    "context",
    "outer_rows_written",
    "component_rows_written",
    "expected_total",
)

FetchPage = Callable[..., TencentBillPage]
FetchMonthSummary = Callable[..., TencentBillMonthSummary]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class SyncTencentBillingSummaryResult:
    account_id: str
    bill_days_completed: tuple[date, ...]
    bill_days_verified: tuple[date, ...]
    outer_rows_seen: int
    component_rows_seen: int
    rows_written: int
    touched_usage_dates: tuple[date, ...]
    dry_run: bool


def run_sync_tencent_billing_summary(
    engine: Engine,
    *,
    settings: TencentBillingSettings,
    bill_day_start: date | None = None,
    bill_day_end: date | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    fetch_page: FetchPage = fetch_tencent_bill_detail_page,
    fetch_month_summary: FetchMonthSummary = fetch_tencent_bill_month_summary,
    sleep: Sleep = time.sleep,
) -> SyncTencentBillingSummaryResult:
    if (bill_day_start is None) != (bill_day_end is None):
        raise ValueError("bill_day_start and bill_day_end must be set together")
    if bill_day_start and bill_day_start > bill_day_end:
        raise ValueError("bill day start must be before or equal to end")
    if dry_run and bill_day_start is None:
        raise ValueError("dry-run requires an explicit bill-day range")

    now = _utc_now(now)
    if dry_run:
        completed: list[date] = []
        outer_rows = 0
        component_rows = 0
        touched: set[date] = set()
        for bill_day in _days(bill_day_start, bill_day_end):
            evidence, usage_dates = _scan_bill_day(
                bill_day,
                settings=settings,
                fetch_page=fetch_page,
                sleep=sleep,
                allow_empty=True,
            )
            completed.append(bill_day)
            outer_rows += int(evidence["outer_row_count"])
            component_rows += int(evidence["component_row_count"])
            touched.update(usage_dates)
        return SyncTencentBillingSummaryResult(
            account_id=settings.account_id,
            bill_days_completed=tuple(completed),
            bill_days_verified=(),
            outer_rows_seen=outer_rows,
            component_rows_seen=component_rows,
            rows_written=0,
            touched_usage_dates=tuple(sorted(touched)),
            dry_run=True,
        )

    explicit_range = bill_day_start is not None
    state_job_name = source_job_name(JOB_NAME, vendor=VENDOR, account_id=settings.account_id)
    if explicit_range:
        state_job_name += f":range:{bill_day_start.isoformat()}:{bill_day_end.isoformat()}"

    with engine.begin() as connection:
        ensure_cost_source_enabled(
            connection,
            vendor=VENDOR,
            account_id=settings.account_id,
            dry_run=False,
            display_name=settings.account_id,
        )
        state = state_store.get_job_state(connection, state_job_name)
        watermark = dict(state.watermark) if state else {}
        if watermark.get("account_id") not in (None, settings.account_id):
            raise ValueError("Tencent billing job state belongs to another account")
        watermark["account_id"] = settings.account_id
        watermark.setdefault("pending_verification", {})
        if explicit_range:
            same_range = (
                watermark.get("range_start") == bill_day_start.isoformat()
                and watermark.get("range_end") == bill_day_end.isoformat()
            )
            if not same_range or not watermark.get("inflight_bill_day"):
                watermark = {
                    "account_id": settings.account_id,
                    "pending_verification": {},
                    "range_start": bill_day_start.isoformat(),
                    "range_end": bill_day_end.isoformat(),
                }
        state_store.mark_job_started(connection, state_job_name, watermark)

    completed_days: list[date] = []
    verified_days: list[date] = []
    touched_usage_dates: set[date] = set()
    outer_rows_seen = 0
    component_rows_seen = 0
    rows_written = 0

    try:
        import_end = (
            bill_day_end
            if explicit_range
            else now.astimezone(_BEIJING).date() - timedelta(days=settings.import_lag_days)
        )
        import_start = bill_day_start if explicit_range else settings.earliest_bill_day
        if import_start is None and not watermark.get("last_completed_bill_day"):
            raise ValueError(
                "COST_INSIGHT_TENCENT_EARLIEST_BILL_DAY is required for the first scheduled run"
            )

        next_day = _next_import_day(
            watermark,
            explicit_start=import_start,
            explicit_range=explicit_range,
        )
        while next_day is not None and next_day <= import_end:
            _start_inflight_day(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                bill_day=next_day,
            )
            evidence, usage_dates = _import_bill_day(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                bill_day=next_day,
                settings=settings,
                fetch_page=fetch_page,
                sleep=sleep,
                completed_at=now,
                allow_empty=explicit_range,
            )
            completed_days.append(next_day)
            touched_usage_dates.update(usage_dates)
            outer_rows_seen += int(evidence["outer_row_count"])
            component_rows_seen += int(evidence["component_row_count"])
            rows_written += int(evidence["component_row_count"])
            next_day += timedelta(days=1)

        if not explicit_range:
            verify_cutoff = now.astimezone(_BEIJING).date() - timedelta(
                days=settings.verify_lag_days
            )
            verified, replayed, replay_usage_dates = _verify_pending_days(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                verify_cutoff=verify_cutoff,
                settings=settings,
                fetch_page=fetch_page,
                sleep=sleep,
                now=now,
            )
            verified_days.extend(verified)
            touched_usage_dates.update(replay_usage_dates)
            for evidence in replayed:
                completed_days.append(date.fromisoformat(str(evidence["bill_day"])))
                outer_rows_seen += int(evidence["outer_row_count"])
                component_rows_seen += int(evidence["component_row_count"])
                rows_written += int(evidence["component_row_count"])

            _reconcile_closed_months(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                settings=settings,
                fetch_month_summary=fetch_month_summary,
                sleep=sleep,
                now=now,
            )

        with engine.begin() as connection:
            state_store.mark_job_succeeded(connection, state_job_name, watermark)
    except Exception as exc:
        LOG.exception("sync_tencent_billing_summary failed")
        with engine.begin() as connection:
            persisted = state_store.get_job_state(connection, state_job_name)
            failed_watermark = persisted.watermark if persisted else watermark
            state_store.mark_job_failed(
                connection,
                state_job_name,
                failed_watermark,
                repr(exc),
            )
        raise

    return SyncTencentBillingSummaryResult(
        account_id=settings.account_id,
        bill_days_completed=tuple(completed_days),
        bill_days_verified=tuple(verified_days),
        outer_rows_seen=outer_rows_seen,
        component_rows_seen=component_rows_seen,
        rows_written=rows_written,
        touched_usage_dates=tuple(sorted(touched_usage_dates)),
        dry_run=False,
    )


def _next_import_day(
    watermark: dict[str, Any],
    *,
    explicit_start: date | None,
    explicit_range: bool,
) -> date | None:
    inflight = watermark.get("inflight_bill_day")
    if inflight:
        return date.fromisoformat(str(inflight))
    completed = watermark.get("last_completed_bill_day")
    if completed:
        return date.fromisoformat(str(completed)) + timedelta(days=1)
    if explicit_start is None and explicit_range:
        return None
    return explicit_start


def _start_inflight_day(
    engine: Engine,
    *,
    state_job_name: str,
    watermark: dict[str, Any],
    bill_day: date,
) -> None:
    if watermark.get("inflight_bill_day"):
        if watermark["inflight_bill_day"] != bill_day.isoformat():
            raise ValueError("Cannot start a bill day while another day is in flight")
        return
    watermark.update(
        {
            "inflight_bill_day": bill_day.isoformat(),
            "next_offset": 0,
            "context": None,
            "outer_rows_written": 0,
            "component_rows_written": 0,
            "expected_total": None,
        }
    )
    with engine.begin() as connection:
        state_store.checkpoint_job_watermark(connection, state_job_name, watermark)


def _import_bill_day(
    engine: Engine,
    *,
    state_job_name: str,
    watermark: dict[str, Any],
    bill_day: date,
    settings: TencentBillingSettings,
    fetch_page: FetchPage,
    sleep: Sleep,
    completed_at: datetime,
    allow_empty: bool = False,
) -> tuple[dict[str, Any], tuple[date, ...]]:
    while True:
        offset = int(watermark.get("next_offset") or 0)
        page = _fetch_with_retry(
            fetch_page,
            bill_day=bill_day,
            offset=offset,
            limit=settings.page_size,
            context=watermark.get("context"),
            need_record_num=offset == 0,
            sleep=sleep,
        )
        summary_rows = tuple(
            _normalize_summary_row(row, preserve_source_row_hash=True)
            for row in expand_tencent_bill_details(
                page.details,
                expected_bill_day=bill_day,
                account_id=settings.account_id,
            )
        )
        if (
            not allow_empty
            and not page.details
            and int(watermark.get("outer_rows_written") or 0) == 0
        ):
            raise ValueError(f"Tencent bill day {bill_day} is empty; refusing to advance")

        final_page = len(page.details) < settings.page_size
        with engine.begin() as connection:
            _write_summary_rows(
                connection,
                summary_rows,
                cleanup_superseded=False,
            )
            watermark["next_offset"] = offset + len(page.details)
            watermark["context"] = page.context
            watermark["outer_rows_written"] = int(
                watermark.get("outer_rows_written") or 0
            ) + len(page.details)
            watermark["component_rows_written"] = int(
                watermark.get("component_rows_written") or 0
            ) + len(summary_rows)
            if watermark.get("expected_total") is None and page.total is not None:
                watermark["expected_total"] = page.total

            if final_page:
                evidence = _partition_evidence(
                    connection,
                    account_id=settings.account_id,
                    bill_day=bill_day,
                    outer_row_count=int(watermark["outer_rows_written"]),
                    expected_total=watermark.get("expected_total"),
                    completed_at=completed_at,
                )
                if evidence["component_row_count"] != int(
                    watermark["component_rows_written"]
                ):
                    raise ValueError(
                        "Tencent component identity collision or stale partition detected: "
                        f"processed={watermark['component_rows_written']}, "
                        f"stored={evidence['component_row_count']}"
                    )
                usage_dates = _partition_usage_dates(
                    connection,
                    account_id=settings.account_id,
                    bill_day=bill_day,
                )
                pending = dict(watermark.get("pending_verification") or {})
                pending[bill_day.isoformat()] = evidence
                watermark["pending_verification"] = pending
                watermark.setdefault("first_completed_bill_day", bill_day.isoformat())
                previous_completed = watermark.get("last_completed_bill_day")
                watermark["last_completed_bill_day"] = max(
                    filter(None, (previous_completed, bill_day.isoformat()))
                )
                for key in _INFLIGHT_KEYS:
                    watermark.pop(key, None)
            state_store.checkpoint_job_watermark(connection, state_job_name, watermark)

        LOG.info(
            "Tencent billing page committed",
            extra={
                "bill_day": bill_day.isoformat(),
                "page_offset": offset,
                "outer_rows": len(page.details),
                "component_rows": len(summary_rows),
                "final_page": final_page,
            },
        )
        if final_page:
            return evidence, usage_dates


def _verify_pending_days(
    engine: Engine,
    *,
    state_job_name: str,
    watermark: dict[str, Any],
    verify_cutoff: date,
    settings: TencentBillingSettings,
    fetch_page: FetchPage,
    sleep: Sleep,
    now: datetime,
) -> tuple[list[date], list[dict[str, Any]], set[date]]:
    verified: list[date] = []
    replayed: list[dict[str, Any]] = []
    touched: set[date] = set()
    pending = dict(watermark.get("pending_verification") or {})
    for day_text in sorted(pending):
        bill_day = date.fromisoformat(day_text)
        if bill_day > verify_cutoff:
            continue
        evidence = dict(pending[day_text])
        probe = _fetch_with_retry(
            fetch_page,
            bill_day=bill_day,
            offset=0,
            limit=1,
            context=None,
            need_record_num=True,
            sleep=sleep,
        )
        if probe.details:
            expand_tencent_bill_details(
                probe.details,
                expected_bill_day=bill_day,
                account_id=settings.account_id,
            )
        if probe.total is None:
            raise ValueError(f"Tencent D+5 probe returned no Total for {bill_day}")

        imported_count = int(evidence["outer_row_count"])
        if probe.total > imported_count:
            _start_inflight_day(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                bill_day=bill_day,
            )
            replay_evidence, usage_dates = _import_bill_day(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                bill_day=bill_day,
                settings=settings,
                fetch_page=fetch_page,
                sleep=sleep,
                completed_at=now,
            )
            replayed.append({"bill_day": bill_day.isoformat(), **replay_evidence})
            touched.update(usage_dates)
            _mark_verified(
                engine,
                state_job_name=state_job_name,
                watermark=watermark,
                bill_day=bill_day,
                observed_total=probe.total,
                status="replayed-increase",
            )
            verified.append(bill_day)
            continue

        if probe.total < imported_count:
            first_seen = evidence.get("smaller_total_first_seen_at")
            if first_seen is None:
                evidence["smaller_total_first_seen_at"] = now.isoformat()
                evidence["last_observed_total"] = probe.total
                pending = dict(watermark.get("pending_verification") or {})
                pending[day_text] = evidence
                watermark["pending_verification"] = pending
                with engine.begin() as connection:
                    state_store.checkpoint_job_watermark(
                        connection,
                        state_job_name,
                        watermark,
                    )
                continue
            if now - datetime.fromisoformat(str(first_seen)) < timedelta(hours=24):
                continue
            scanned, _ = _scan_bill_day(
                bill_day,
                settings=settings,
                fetch_page=fetch_page,
                sleep=sleep,
            )
            if not _same_completion_evidence(evidence, scanned):
                raise ValueError(
                    f"Tencent bill day {bill_day} changed after completion; explicit repair required"
                )
            status = "verified-stale-total"
        else:
            status = "verified-count"

        _mark_verified(
            engine,
            state_job_name=state_job_name,
            watermark=watermark,
            bill_day=bill_day,
            observed_total=probe.total,
            status=status,
        )
        verified.append(bill_day)
    return verified, replayed, touched


def _mark_verified(
    engine: Engine,
    *,
    state_job_name: str,
    watermark: dict[str, Any],
    bill_day: date,
    observed_total: int,
    status: str,
) -> None:
    pending = dict(watermark.get("pending_verification") or {})
    pending.pop(bill_day.isoformat(), None)
    watermark["pending_verification"] = pending
    previous = watermark.get("last_verified_bill_day")
    watermark["last_verified_bill_day"] = max(
        filter(None, (previous, bill_day.isoformat()))
    )
    watermark["last_verification"] = {
        "bill_day": bill_day.isoformat(),
        "observed_total": observed_total,
        "status": status,
    }
    with engine.begin() as connection:
        state_store.checkpoint_job_watermark(connection, state_job_name, watermark)


def _scan_bill_day(
    bill_day: date,
    *,
    settings: TencentBillingSettings,
    fetch_page: FetchPage,
    sleep: Sleep,
    allow_empty: bool = False,
) -> tuple[dict[str, Any], tuple[date, ...]]:
    offset = 0
    context: str | None = None
    outer_count = 0
    rows: list[dict[str, Any]] = []
    while True:
        page = _fetch_with_retry(
            fetch_page,
            bill_day=bill_day,
            offset=offset,
            limit=settings.page_size,
            context=context,
            need_record_num=offset == 0,
            sleep=sleep,
        )
        expanded = expand_tencent_bill_details(
            page.details,
            expected_bill_day=bill_day,
            account_id=settings.account_id,
        )
        rows.extend(expanded)
        outer_count += len(page.details)
        if len(page.details) < settings.page_size:
            break
        offset += len(page.details)
        context = page.context
    if outer_count == 0 and not allow_empty:
        raise ValueError(f"Tencent bill day {bill_day} is empty; refusing to advance")
    hashes = [str(row["source_row_hash"]) for row in rows]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Tencent component identity collision across pages")
    return (
        _evidence_from_rows(rows, outer_row_count=outer_count),
        tuple(sorted({row["usage_date"] for row in rows})),
    )


def _fetch_with_retry(
    fetch_page: FetchPage,
    *,
    bill_day: date,
    offset: int,
    limit: int,
    context: str | None,
    need_record_num: bool,
    sleep: Sleep,
) -> TencentBillPage:
    contexts = (context, None) if context else (None,)
    last_error: Exception | None = None
    for candidate_context in contexts:
        for attempt in range(3):
            try:
                return fetch_page(
                    bill_day=bill_day,
                    offset=offset,
                    limit=limit,
                    context=candidate_context,
                    need_record_num=need_record_num,
                )
            except Exception as exc:  # SDK error types vary by transport and API code.
                last_error = exc
                if attempt < 2:
                    sleep(2**attempt)
        if candidate_context:
            LOG.warning(
                "Tencent billing Context rejected after retries; retrying offset without Context",
                extra={"bill_day": bill_day.isoformat(), "offset": offset},
            )
    assert last_error is not None
    raise last_error


def _partition_evidence(
    connection: Connection,
    *,
    account_id: str,
    bill_day: date,
    outer_row_count: int,
    expected_total: int | None,
    completed_at: datetime,
) -> dict[str, Any]:
    rows = connection.execute(
        text(
            """
            SELECT source_row_hash, list_cost, effective_cost, net_cost, source_export_time
            FROM cost_bq_export_summary_daily
            WHERE vendor = 'tencent'
              AND account_id = :account_id
              AND export_partition_date = :bill_day
            ORDER BY source_row_hash
            """
        ),
        {"account_id": account_id, "bill_day": bill_day},
    ).mappings()
    evidence = _evidence_from_rows(list(rows), outer_row_count=outer_row_count)
    evidence["expected_total"] = expected_total
    evidence["completed_at"] = completed_at.isoformat()
    return evidence


def _evidence_from_rows(
    rows: list[dict[str, Any]] | list[Any],
    *,
    outer_row_count: int,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256()
    list_total = Decimal(0)
    effective_total = Decimal(0)
    net_total = Decimal(0)
    latest_pay_time: str | None = None
    for row in sorted(rows, key=lambda item: str(item["source_row_hash"])):
        fingerprint.update(str(row["source_row_hash"]).encode("ascii"))
        fingerprint.update(b"\n")
        list_total += _as_decimal(row.get("list_cost"))
        effective_total += _as_decimal(row.get("effective_cost"))
        net_total += _as_decimal(row.get("net_cost"))
        value = row.get("source_export_time")
        if value is not None:
            candidate = _datetime_text(value)
            latest_pay_time = max(filter(None, (latest_pay_time, candidate)))
    return {
        "outer_row_count": outer_row_count,
        "component_row_count": len(rows),
        "identity_fingerprint": fingerprint.hexdigest(),
        "list_cost_cny": _decimal_text(list_total),
        "effective_cost_cny": _decimal_text(effective_total),
        "net_cost_cny": _decimal_text(net_total),
        "latest_pay_time": latest_pay_time,
    }


def _partition_usage_dates(
    connection: Connection,
    *,
    account_id: str,
    bill_day: date,
) -> tuple[date, ...]:
    values = connection.execute(
        text(
            """
            SELECT DISTINCT usage_date
            FROM cost_bq_export_summary_daily
            WHERE vendor = 'tencent'
              AND account_id = :account_id
              AND export_partition_date = :bill_day
            ORDER BY usage_date
            """
        ),
        {"account_id": account_id, "bill_day": bill_day},
    ).scalars()
    return tuple(date.fromisoformat(str(value)) for value in values)


def _same_completion_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in (
        "outer_row_count",
        "component_row_count",
        "identity_fingerprint",
        "latest_pay_time",
    ):
        if left.get(key) != right.get(key):
            return False
    return all(
        abs(_as_decimal(left.get(key)) - _as_decimal(right.get(key))) <= _AMOUNT_QUANTUM
        for key in ("list_cost_cny", "effective_cost_cny", "net_cost_cny")
    )


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _datetime_text(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.isoformat(timespec="seconds")


def _reconcile_closed_months(
    engine: Engine,
    *,
    state_job_name: str,
    watermark: dict[str, Any],
    settings: TencentBillingSettings,
    fetch_month_summary: FetchMonthSummary,
    sleep: Sleep,
    now: datetime,
) -> None:
    """Reconcile complete closed months without blocking daily imports on an unready summary."""
    with engine.begin() as connection:
        usage_bounds = connection.execute(
            text(
                """
                SELECT MIN(usage_date) AS min_usage_date, MAX(usage_date) AS max_usage_date
                FROM cost_bq_export_summary_daily
                WHERE vendor = 'tencent' AND account_id = :account_id
                """
            ),
            {"account_id": settings.account_id},
        ).mappings().one()
    min_usage = _as_date(usage_bounds["min_usage_date"])
    max_usage = _as_date(usage_bounds["max_usage_date"])
    if min_usage is None or max_usage is None:
        return

    reconciled = dict(watermark.get("reconciled_months") or {})
    completed_backfills = _completed_range_backfills(engine, state_job_name=state_job_name)
    scheduled_coverage_start = _as_date(watermark.get("first_completed_bill_day"))
    beijing_now = now.astimezone(_BEIJING)
    month = min_usage.replace(day=1)
    last_month = max_usage.replace(day=1)
    while month <= last_month:
        month_key = f"{month.year:04d}-{month.month:02d}"
        record = dict(reconciled.get(month_key) or {})
        if beijing_now < _month_close_time(month):
            month = _next_month(month)
            continue
        if record.get("status") in _MATCHED_MONTH_STATUSES:
            month = _next_month(month)
            continue

        with engine.begin() as connection:
            imported = connection.execute(
                text(
                    """
                    SELECT
                      COALESCE(SUM(list_cost), 0) AS list_cost,
                      COALESCE(SUM(net_cost), 0) AS net_cost,
                      MIN(export_partition_date) AS first_export_partition_date
                    FROM cost_bq_export_summary_daily
                    WHERE vendor = 'tencent'
                      AND account_id = :account_id
                      AND usage_date BETWEEN :month_start AND :month_end
                    """
                ),
                {
                    "account_id": settings.account_id,
                    "month_start": month,
                    "month_end": _month_end(month),
                },
            ).mappings().one()
        imported_list = _as_decimal(imported["list_cost"])
        imported_net = _as_decimal(imported["net_cost"])
        coverage_start = _month_coverage_start(
            month,
            scheduled_coverage_start=scheduled_coverage_start,
            first_export_partition_date=_as_date(imported["first_export_partition_date"]),
            completed_range_covers_month=_range_covers_month(completed_backfills, month),
        )
        coverage_start_text = coverage_start.isoformat() if coverage_start is not None else None
        if (
            record.get("status") == "partial-coverage"
            and record.get("coverage_start") == coverage_start_text
        ):
            month = _next_month(month)
            continue
        if coverage_start is None or _month_precedes_coverage(month, coverage_start):
            reconciled[month_key] = {
                "status": "partial-coverage",
                "coverage_start": coverage_start_text,
            }
            watermark["reconciled_months"] = reconciled
            with engine.begin() as connection:
                state_store.checkpoint_job_watermark(connection, state_job_name, watermark)
            month = _next_month(month)
            continue
        if (
            record.get("status") == "mismatch"
            and record.get("summary_ready") is not False
            and _same_imported_month_totals(
                record,
                imported_list=imported_list,
                imported_net=imported_net,
            )
        ):
            month = _next_month(month)
            continue

        summary = _fetch_month_summary_with_retry(
            fetch_month_summary,
            month=month_key,
            sleep=sleep,
        )
        if summary is None:
            reconciled[month_key] = {
                **record,
                "status": record.get("status") if record.get("status") == "mismatch" else "unready",
                "checked_at": now.isoformat(),
                "summary_ready": False,
            }
            watermark["reconciled_months"] = reconciled
            with engine.begin() as connection:
                state_store.checkpoint_job_watermark(connection, state_job_name, watermark)
            LOG.warning("Tencent month-close summary is not ready", extra={"month": month_key})
            month = _next_month(month)
            continue

        mismatches = []
        if summary.total_cost is not None and imported_list != summary.total_cost:
            mismatches.append(f"list {imported_list} != {summary.total_cost}")
        if imported_net != summary.real_total_cost:
            mismatches.append(f"net {imported_net} != {summary.real_total_cost}")
        matched = not mismatches
        reconciled[month_key] = {
            "status": (
                "matched-real-cost-only"
                if matched and summary.total_cost is None
                else "matched" if matched else "mismatch"
            ),
            "source_total_cost": (
                _decimal_text(summary.total_cost) if summary.total_cost is not None else None
            ),
            "source_real_total_cost": _decimal_text(summary.real_total_cost),
            "imported_list_cost": _decimal_text(imported_list),
            "imported_net_cost": _decimal_text(imported_net),
        }
        watermark["reconciled_months"] = reconciled
        with engine.begin() as connection:
            state_store.checkpoint_job_watermark(connection, state_job_name, watermark)
        if not matched:
            raise ValueError(
                f"Tencent month-close reconciliation failed for {month_key}: "
                f"{', '.join(mismatches)}; explicit repair required"
            )
        LOG.info(
            "Tencent month-close reconciliation matched",
            extra={"month": month_key, **reconciled[month_key]},
        )
        month = _next_month(month)


def _completed_range_backfills(
    engine: Engine,
    *,
    state_job_name: str,
) -> tuple[tuple[date, date], ...]:
    range_prefix = f"{state_job_name}:range:"
    with engine.connect() as connection:
        rows = tuple(
            connection.execute(
                text(
                    """
                    SELECT job_name, watermark_json
                    FROM cost_job_state
                    WHERE last_status = 'succeeded'
                      AND SUBSTR(job_name, 1, :prefix_length) = :range_prefix
                    """
                ),
                {"range_prefix": range_prefix, "prefix_length": len(range_prefix)},
            ).mappings()
        )
    completed: list[tuple[date, date]] = []
    for row in rows:
        try:
            watermark = state_store._parse_watermark(row["watermark_json"])
        except ValueError:
            LOG.warning(
                "Ignoring malformed Tencent range-backfill state",
                extra={"job_name": row["job_name"]},
            )
            continue
        range_start = _as_date(watermark.get("range_start"))
        range_end = _as_date(watermark.get("range_end"))
        last_completed = _as_date(watermark.get("last_completed_bill_day"))
        if (
            range_start is not None
            and range_end is not None
            and last_completed is not None
            and last_completed >= range_end
        ):
            completed.append((range_start, range_end))
    return tuple(completed)


def _range_covers_month(ranges: tuple[tuple[date, date], ...], month: date) -> bool:
    return any(
        range_start <= month and range_end >= _month_end(month)
        for range_start, range_end in ranges
    )


def _month_coverage_start(
    month: date,
    *,
    scheduled_coverage_start: date | None,
    first_export_partition_date: date | None,
    completed_range_covers_month: bool,
) -> date | None:
    starts = [first_export_partition_date, month if completed_range_covers_month else None]
    if scheduled_coverage_start is not None and scheduled_coverage_start <= _month_end(month):
        starts.append(scheduled_coverage_start)
    available = [start for start in starts if start is not None]
    return min(available) if available else None


def _month_precedes_coverage(month: date, coverage_start: date | None) -> bool:
    if coverage_start is None:
        return False
    coverage_month = coverage_start.replace(day=1)
    return month < coverage_month or (month == coverage_month and coverage_start.day != 1)


def _same_imported_month_totals(
    record: dict[str, Any],
    *,
    imported_list: Decimal,
    imported_net: Decimal,
) -> bool:
    return (
        record.get("imported_list_cost") == _decimal_text(imported_list)
        and record.get("imported_net_cost") == _decimal_text(imported_net)
    )


def _fetch_month_summary_with_retry(
    fetch_month_summary: FetchMonthSummary,
    *,
    month: str,
    sleep: Sleep,
) -> TencentBillMonthSummary | None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return fetch_month_summary(month=month)
        except TencentBillSummaryNotReady:
            return None
        except Exception as exc:  # SDK error types vary by transport and API code.
            last_error = exc
            if attempt < 2:
                sleep(2**attempt)
    assert last_error is not None
    raise last_error


def _month_close_time(month: date) -> datetime:
    """Beijing time after which a month's bill is considered closed: 19:00 on the 1st."""
    next_month = _next_month(month)
    return datetime(next_month.year, next_month.month, 1, 19, 0, tzinfo=_BEIJING)


def _month_end(month: date) -> date:
    return _next_month(month) - timedelta(days=1)


def _next_month(month: date) -> date:
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _days(start: date | None, end: date | None):
    assert start is not None and end is not None
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
