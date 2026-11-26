"""Common warehouse execution: no live ADC, services or production database."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, text

from cost_insight.common.config import load_settings
from cost_insight.jobs import cli, state_store
from cost_insight.jobs import sync_warehouse_billing_summary as job
from cost_insight.jobs.sync_gcp_billing_summary import write_summary_rows
from cost_insight.sources.warehouse_billing_export import WarehouseWindow

START = date(2026, 10, 25)
END = date(2026, 10, 29)
PROJECT = "warehouse-f80fbbe8e8"
ACCOUNT = "account-a"
DORMANT = "account-not-requested"
RULES = "tcms_cost.resource_allocation"


def raw(vendor, account=ACCOUNT, day=END, **extra):
    return {"vendor": vendor, "account_id": account, "billing_account_id": "billing",
            "export_partition_date": day.replace(day=1), "usage_date": day, "currency": "USD",
            "service_name": "compute", "sku_name": "instance", "usage_type": "Usage",
            "list_cost": Decimal(10), "effective_cost": Decimal(8), "credit_amount": Decimal(0),
            "net_cost": Decimal(8), "source_export_time": datetime(2026, 10, 30, tzinfo=UTC), **extra}


def window(vendor, account=ACCOUNT, *, start=START, end=END):
    rows = tuple(raw(vendor, account, day) for day in job._dates(start, end))
    controls = tuple({"export_partition_date": r["export_partition_date"], "usage_date": r["usage_date"],
                      "n": 1, "non_usd": 0, "invalid_money": 0,
                      **{m: r[m] for m in job.METRICS}} for r in rows)
    return WarehouseWindow(start=start, end=end, rows=rows, controls=controls,
                           snapshots={"source": ("fixed", 100, "etag")})


def normalized(vendor, account=ACCOUNT, day=END, **extra):
    fn = job.normalize_azure if vendor == "azure" else job.normalize_aws
    return fn(raw(vendor, account, day, **extra))


@pytest.fixture(params=["aws", "azure"])
def vendor(request):
    return request.param


@pytest.fixture
def engine(vendor, monkeypatch):
    db = create_engine("sqlite://")
    fields = {**job.normalize_aws(raw("aws")), **job.normalize_azure(raw("azure"))}
    cols = ",".join(f'"{k}" ' + ("REAL" if k in job.METRICS else "TEXT") for k in fields)
    with db.begin() as c:
        c.execute(text(f'''CREATE TABLE {job.SUMMARY} ({cols}, updated_at TEXT,
            UNIQUE(vendor,account_id,export_partition_date,source_row_hash))'''))
        c.execute(text('''CREATE TABLE cost_sources(vendor TEXT,account_id TEXT,billing_account_id TEXT,
            display_name TEXT,is_active INTEGER,source_table TEXT,source_schema_version TEXT,
            source_available_from TEXT,UNIQUE(vendor,account_id))'''))
        c.execute(text('''CREATE TABLE cost_job_state(job_name TEXT PRIMARY KEY,watermark_json TEXT,
            last_started_at TEXT,last_succeeded_at TEXT,last_status TEXT,last_error TEXT,updated_at TEXT)'''))
        for table in ("cost_attribution_daily", "cost_resource_serving_daily"):
            c.execute(text(f'''CREATE TABLE {table}(vendor TEXT,account_id TEXT,usage_date TEXT,currency TEXT,
                basis_key TEXT,materialization_version TEXT,list_cost REAL,effective_cost REAL,
                credit_amount REAL,net_cost REAL)'''))
        c.execute(text('''CREATE TABLE cost_resource_serving_publication(basis_key TEXT,vendor TEXT,
            account_id TEXT,usage_date TEXT,currency TEXT,active_materialization_version TEXT,total_list_cost REAL)'''))
        for account in (ACCOUNT, "account-b", DORMANT):
            c.execute(text("INSERT INTO cost_sources(vendor,account_id,is_active) VALUES(:v,:a,1)"),
                      {"v": vendor, "a": account})
    write_summary_rows(db, [normalized(vendor, a, START - timedelta(days=1))
                           for a in (ACCOUNT, "account-b", DORMANT)], dry_run=False)
    monkeypatch.setattr(job, "_validate_tcms_allocation_table", Mock())
    monkeypatch.setattr(job, "assert_snapshot", Mock())
    monkeypatch.setattr(job, "fetch_warehouse_window", lambda client, **kw: window(kw["vendor"], kw["account_id"]))
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish)
    yield db
    db.dispose()


def publish(engine, *, source, start_date, end_date, tcms_allocation_table):
    assert start_date == end_date and tcms_allocation_table == RULES
    params = {"vendor": source.vendor, "account": source.account_id, "day": start_date}
    with engine.begin() as c:
        total = job._table_totals(c, job.SUMMARY, source.vendor, source.account_id, start_date)
        params.update({m: total[m] for m in job.METRICS})
        for table in ("cost_attribution_daily", "cost_resource_serving_daily"):
            c.execute(text(f"DELETE FROM {table} WHERE vendor=:vendor AND account_id=:account AND usage_date=:day"), params)
            c.execute(text(f'''INSERT INTO {table} VALUES(:vendor,:account,:day,'USD','native','active',
                :list_cost,:effective_cost,:credit_amount,:net_cost)'''),
                {k: float(v) if isinstance(v, Decimal) else v for k, v in params.items()})
        c.execute(text("DELETE FROM cost_resource_serving_publication WHERE vendor=:vendor AND account_id=:account AND usage_date=:day"), params)
        c.execute(text('''INSERT INTO cost_resource_serving_publication VALUES
            ('native',:vendor,:account,:day,'USD','active',:list_cost)'''),
            {k: float(v) if isinstance(v, Decimal) else v for k, v in params.items()})


def run(engine, vendor, **extra):
    return job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor=vendor,
        account_ids=[ACCOUNT], project=PROJECT, tcms_allocation_table=RULES, **extra)


def job_name(vendor, account=ACCOUNT):
    base = f"sync_{vendor}_warehouse_daily"
    return f"{base}:aws:{account}" if vendor == "aws" else base


def state(engine, vendor, account=ACCOUNT):
    with engine.connect() as c:
        return state_store.get_job_state(c, job_name(vendor, account))


def test_both_routes_catch_up_and_publish_without_touching_unrequested_identity(engine, vendor):
    write_summary_rows(engine, [normalized(vendor, day=START + timedelta(days=1), repo="obsolete")], dry_run=False)
    result = run(engine, vendor)
    assert result["percent"] == 100 and len(result["completed_account_days"]) == 5
    assert state(engine, vendor).last_status == "succeeded"
    with engine.connect() as c:
        assert c.execute(text(f"SELECT MAX(usage_date) FROM {job.SUMMARY} WHERE account_id=:a"), {"a": ACCOUNT}).scalar_one() == str(END)
        assert c.execute(text(f"SELECT COUNT(*) FROM {job.SUMMARY} WHERE repo='obsolete'")).scalar_one() == 0
        assert c.execute(text(f"SELECT COUNT(*) FROM {job.SUMMARY} WHERE account_id=:a"), {"a": DORMANT}).scalar_one() == 1
        for day in job._dates(START, END):
            job._verify_publication(c, vendor, ACCOUNT, day)


def test_dry_run_is_select_only_even_with_existing_failed_latch(engine, vendor):
    with engine.begin() as c:
        state_store.mark_job_failed(c, job_name(vendor), {"run_id": "old"}, "existing failure")
    old = state(engine, vendor)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql.strip().upper()))
    result = run(engine, vendor, dry_run=True)
    assert len(result["windows"]) == 1 and not result["completed_account_days"]
    assert statements and all(s.startswith("SELECT") for s in statements)
    assert state(engine, vendor) == old


def test_preserves_sibling_day_and_other_billing_partition(engine, vendor):
    sibling = normalized(vendor, export_partition_date=date(2026, 9, 1))
    write_summary_rows(engine, [sibling], dry_run=False)
    run(engine, vendor)
    with engine.connect() as c:
        old = job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, END, partition=date(2026, 9, 1))
        assert old["n"] == 1 and old["list_cost"] == 10
        assert job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, START - timedelta(days=1))["n"] == 1
        assert job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, END)["list_cost"] == 20
        job._verify_publication(c, vendor, ACCOUNT, END)


def test_canonical_collisions_merge_all_four_money_fields(vendor):
    w = window(vendor)
    first = {**w.rows[0], "vendor_tags_json": '{"tenant":"QE","cluster":"c"}'}
    second = {**first, "vendor_tags_json": '{"cluster":"c","tenant":"QE"}',
              "source_export_time": first["source_export_time"] + timedelta(hours=1)}
    rows = (first, *w.rows[1:], second)
    controls = ({**w.controls[0], **{m: w.controls[0][m] * 2 for m in job.METRICS}}, *w.controls[1:])
    groups = job._normalize(WarehouseWindow(w.start, w.end, rows, controls, w.snapshots), vendor, ACCOUNT)
    merged = groups[START.replace(day=1), START]
    assert len(merged) == 1 and merged[0]["list_cost"] == 20 and merged[0]["net_cost"] == 16
    assert merged[0]["source_export_time"] == second["source_export_time"].replace(tzinfo=None)
    if vendor == "azure":
        assert merged[0]["org"] == "QE"


@pytest.mark.parametrize("field,value", [("currency", "EUR"), ("account_id", DORMANT),
    ("source_schema_version", "split"), ("source_allocation_scope", "split"), ("net_cost", None),
    ("net_cost", "NaN"), ("usage_date", date(2026, 9, 1)), ("export_partition_date", date(2026, 9, 1))])
def test_invalid_source_fails_before_any_fact_changes(engine, vendor, monkeypatch, field, value):
    w = window(vendor)
    broken = WarehouseWindow(w.start, w.end, ({**w.rows[0], field: value}, *w.rows[1:]), w.controls, w.snapshots)
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(return_value=broken))
    replacement = Mock()
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    with pytest.raises((ValueError, TypeError)):
        run(engine, vendor)
    replacement.assert_not_called()
    assert state(engine, vendor).last_status == "failed"


@pytest.mark.parametrize("field,value", [("n", 0), ("non_usd", 1), ("invalid_money", 1), ("list_cost", 11)])
def test_raw_control_disagreement_and_empty_days_rejected(engine, vendor, monkeypatch, field, value):
    w = window(vendor)
    broken = WarehouseWindow(w.start, w.end, w.rows, ({**w.controls[0], field: value}, *w.controls[1:]), w.snapshots)
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(return_value=broken))
    with pytest.raises(ValueError):
        run(engine, vendor)
    assert state(engine, vendor).last_status == "failed"
    with pytest.raises(ValueError, match="coverage|Empty"):
        job._normalize(WarehouseWindow(w.start, w.end, w.rows[1:], w.controls[1:], w.snapshots), vendor, ACCOUNT)


def test_all_accounts_prefetched_and_changed_shared_snapshot_refuses_all_writes(engine, vendor, monkeypatch):
    calls = []
    def fetch(client, **kw):
        calls.append(kw["account_id"])
        w = window(vendor, kw["account_id"])
        if len(calls) == 2:
            return WarehouseWindow(w.start, w.end, w.rows, w.controls, {"source": ("changed", 101, "etag")})
        return w
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    replacement = Mock()
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    with pytest.raises(ValueError, match="between account"):
        job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor=vendor, account_ids=[ACCOUNT, "account-b"],
            project=PROJECT, tcms_allocation_table=RULES)
    assert calls == [ACCOUNT, "account-b"]
    replacement.assert_not_called()


def test_cli_success_without_native_publication_stops_and_latches(engine, vendor, monkeypatch):
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", Mock())
    with pytest.raises(ValueError, match="Missing warehouse"):
        run(engine, vendor)
    failed = state(engine, vendor)
    assert failed.last_status == "failed" and not failed.watermark["completed_account_days"]
    assert failed.watermark["current_account_day"] == f"{ACCOUNT}/{START}"
    with engine.connect() as c:
        assert c.execute(text(f"SELECT COUNT(DISTINCT usage_date) FROM {job.SUMMARY} WHERE account_id=:a"), {"a": ACCOUNT}).scalar_one() == 2
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish)
    recovered = run(engine, vendor)
    assert recovered["recovered_from_run_id"] == failed.watermark["run_id"]
    assert len(recovered["completed_account_days"]) == 5
    assert state(engine, vendor).last_status == "succeeded"


def test_transport_retry_is_scoped_to_summary_not_whole_warehouse_pipeline(engine, vendor, monkeypatch):
    from pymysql.err import OperationalError

    from cost_insight.common import db_retry

    monkeypatch.setattr(db_retry.time, "sleep", Mock())
    fetch = Mock(side_effect=lambda client, **kw: window(kw["vendor"], kw["account_id"]))
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    failed = False

    def fault(connection, cursor, statement, params, context, many):
        nonlocal failed
        if not failed and statement.replace("`", "").lstrip().startswith(f"INSERT INTO {job.SUMMARY}"):
            failed = True
            raise OperationalError(2013, "Lost connection during summary insert")

    event.listen(engine, "before_cursor_execute", fault)
    result = run(engine, vendor)
    assert failed and result["percent"] == 100
    fetch.assert_called_once()
    db_retry.time.sleep.assert_called_once()
    assert state(engine, vendor).last_status == "succeeded"
    with engine.connect() as c:
        for day in job._dates(START, END):
            total = job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, day)
            assert total["n"] == 1 and total["net_cost"] == 8
            job._verify_publication(c, vendor, ACCOUNT, day)


def test_failure_bookkeeping_disconnect_preserves_original_error(engine, vendor, monkeypatch):
    from pymysql.err import OperationalError

    original = RuntimeError("source validation failed")
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(side_effect=original))
    monkeypatch.setattr(job, "_checkpoint", Mock(side_effect=OperationalError(2013, "Lost connection")))
    with pytest.raises(RuntimeError) as raised:
        run(engine, vendor)
    assert raised.value is original
    assert state(engine, vendor).last_status == "running"
    job._checkpoint.assert_called_once()
    assert job._checkpoint.call_args.args[3] == "failed"


def test_terminal_commit_ack_loss_keeps_succeeded_latch(engine, vendor, monkeypatch):
    from pymysql.err import OperationalError

    original = OperationalError(2013, "Lost commit acknowledgement")
    checkpoint = job._checkpoint

    def lose_ack(*args, **kwargs):
        checkpoint(*args, **kwargs)
        if len(args) > 3 and args[3] == "succeeded":
            # The real checkpoint has already committed before the ACK is lost.
            raise original

    monkeypatch.setattr(job, "_checkpoint", lose_ack)
    with pytest.raises(OperationalError) as raised:
        run(engine, vendor)
    assert raised.value is original
    assert state(engine, vendor).last_status == "succeeded"
    assert state(engine, vendor).watermark["phase"] == "complete"
    assert len(state(engine, vendor).watermark["completed_account_days"]) == 5


def test_running_legacy_operational_key_cannot_be_bypassed(engine, vendor):
    with engine.begin() as c:
        state_store.mark_job_started(c, f"sync_{vendor}_warehouse_daily", {"run_id": "legacy-configmap-worker"})
    with pytest.raises(ValueError, match="running|pod_name"):
        run(engine, vendor)
    with engine.connect() as c:
        legacy = state_store.get_job_state(c, f"sync_{vendor}_warehouse_daily")
    assert legacy.watermark["run_id"] == "legacy-configmap-worker"


@pytest.mark.parametrize("field,value", [("is_active", 0), ("source_schema_version", "aws_split_cost_v1"),
    ("source_table", "gcp-digital-bi.old.source"), ("source_available_from", "2026-10-26")])
def test_registry_scope_and_availability_guard(engine, vendor, field, value):
    with engine.begin() as c:
        c.execute(text(f"UPDATE cost_sources SET {field}=:v WHERE account_id=:a"), {"v": value, "a": ACCOUNT})
    with pytest.raises(ValueError):
        run(engine, vendor)


def test_availability_and_sparse_history_are_not_custom_scope(engine, vendor):
    with engine.begin() as c:
        c.execute(text("UPDATE cost_sources SET source_available_from='2026-06-01' WHERE account_id=:a"), {"a": ACCOUNT})
    write_summary_rows(engine, [normalized(vendor, day=date(2026, 6, 1))], dry_run=False)
    assert run(engine, vendor)["percent"] == 100


def test_excess_backlog_is_not_silently_skipped(engine, vendor):
    with engine.begin() as c:
        c.execute(text(f"DELETE FROM {job.SUMMARY} WHERE account_id=:a"), {"a": ACCOUNT})
    write_summary_rows(engine, [normalized(vendor, day=START - timedelta(days=2))], dry_run=False)
    with pytest.raises(ValueError, match="backlog"):
        run(engine, vendor)


F04_ACCOUNT = "380838443567"
F04_SCHEMA = "aws_tidb_cloud_f04_v1"
F04_USEDBY = "prod-us-west-2-f04"


@pytest.fixture
def f04_engine(engine, vendor, monkeypatch):
    if vendor != "aws":
        pytest.skip("F04 is an AWS-only profile")
    with engine.begin() as c:
        c.execute(text("UPDATE cost_sources SET account_id=:f04, source_schema_version=:profile, "
                       "source_table=:table WHERE account_id=:old"),
                  {"f04": F04_ACCOUNT, "profile": F04_SCHEMA,
                   "table": job.source_table("aws", PROJECT), "old": ACCOUNT})
        c.execute(text(f"UPDATE {job.SUMMARY} SET account_id=:f04 WHERE account_id=:old"),
                  {"f04": F04_ACCOUNT, "old": ACCOUNT})
    def fetch(client, **kw):
        assert kw["f04"] is True and kw["account_id"] == F04_ACCOUNT
        first, last = kw.get("recovery_window") or (kw.get("start_date") or START, kw.get("end_date") or END)
        w = window("aws", F04_ACCOUNT, start=first, end=last)
        rows = [{**r, "author": F04_USEDBY,
                 "vendor_tags_json": '{"usedby":"prod-us-west-2-f04"}'} for r in w.rows]
        return WarehouseWindow(w.start, w.end, rows, w.controls, w.snapshots)
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    yield engine


def run_f04(engine, **extra):
    return job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor="aws",
        account_ids=[F04_ACCOUNT], project=PROJECT, tcms_allocation_table=RULES, **extra)


def test_f04_warehouse_preserves_scope_hash_and_separate_state(f04_engine):
    with f04_engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", {"run_id": "ordinary"}, "held")
    result = run_f04(f04_engine)
    assert result["percent"] == 100
    with f04_engine.connect() as c:
        state = state_store.get_job_state(c, "sync_aws_f04_warehouse_daily")
        assert state.last_status == "succeeded"
        assert state_store.get_job_state(c, "sync_aws_warehouse_daily").last_status == "failed"
        rows = c.execute(text(f"SELECT * FROM {job.SUMMARY} WHERE account_id=:a AND usage_date=:d"),
                         {"a": F04_ACCOUNT, "d": END}).mappings().all()
        assert len(rows) == 1 and rows[0]["author"] == F04_USEDBY
        assert rows[0]["source_schema_version"] is None
        assert rows[0]["source_allocation_scope"] == "direct"
        assert job._table_totals(c, job.SUMMARY, "aws", DORMANT, START - timedelta(days=1))["n"] == 1


@pytest.mark.parametrize("days", [1, 3, 5])
def test_f04_bounded_historical_window_replaces_only_requested_days(f04_engine, days):
    last = START + timedelta(days=days-1)
    result = run_f04(f04_engine, start_date=START, end_date=last)
    assert len(result["completed_account_days"]) == days and result["percent"] == 100
    with f04_engine.connect() as c:
        assert job._table_totals(c, job.SUMMARY, "aws", F04_ACCOUNT, START - timedelta(days=1))["n"] == 1
        assert job._table_totals(c, job.SUMMARY, "aws", F04_ACCOUNT, last + timedelta(days=1))["n"] == 0


@pytest.mark.parametrize("dates", [
    {"start_date": START}, {"end_date": END},
    {"start_date": END, "end_date": START},
    {"start_date": START, "end_date": START + timedelta(days=5)},
])
def test_f04_repair_requires_complete_at_most_five_day_bounds(f04_engine, dates):
    with pytest.raises(ValueError):
        run_f04(f04_engine, **dates)


def test_f04_dry_run_is_select_only_and_failed_run_cannot_be_replayed(f04_engine):
    with f04_engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_f04_warehouse_daily", {"run_id": "held"}, "uncertain write")
    statements = []
    event.listen(f04_engine, "before_cursor_execute",
                 lambda c, cur, sql, p, ctx, many: statements.append(sql.strip().upper()))
    result = run_f04(f04_engine, start_date=START, end_date=START, dry_run=True)
    assert result["dry_run"] and not result["completed_account_days"]
    assert statements and all(s.startswith("SELECT") for s in statements)
    with pytest.raises(ValueError, match="inspect before recovery"):
        run_f04(f04_engine, start_date=START, end_date=START)
    with f04_engine.connect() as c:
        held = state_store.get_job_state(c, "sync_aws_f04_warehouse_daily")
        assert held.last_status == "failed" and held.watermark["run_id"] == "held"


def test_f04_still_requires_contiguous_backlog_recovery(f04_engine):
    with pytest.raises(ValueError, match="backlog"):
        run_f04(f04_engine, start_date=START + timedelta(days=10), end_date=START + timedelta(days=12))


@pytest.mark.parametrize("bad", ["another-tenant", None])
def test_f04_rejects_unscoped_source_rows_before_writing(f04_engine, monkeypatch, bad):
    w = window("aws", F04_ACCOUNT)
    rows = [{**r, "author": bad, "vendor_tags_json": '{"usedby":"wrong"}'} for r in w.rows]
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(return_value=WarehouseWindow(
        w.start, w.end, rows, w.controls, w.snapshots)))
    replacement = Mock()
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    with pytest.raises(ValueError, match="F04"):
        run_f04(f04_engine)
    replacement.assert_not_called()


def test_f04_legacy_table_and_mixed_account_requests_are_rejected(f04_engine):
    with pytest.raises(ValueError, match="mix|separate"):
        job.run_sync_warehouse_billing_summary(f04_engine, client=Mock(), vendor="aws",
            account_ids=[F04_ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES)
    with f04_engine.begin() as c:
        c.execute(text("UPDATE cost_sources SET source_table='gcp-digital-bi.aws_prod_billing.aws_prod_billing_data' "
                       "WHERE account_id=:a"), {"a": F04_ACCOUNT})
    with pytest.raises(ValueError):
        run_f04(f04_engine)


def test_f04_profile_cannot_be_used_for_other_accounts(engine, vendor):
    with engine.begin() as c:
        c.execute(text("UPDATE cost_sources SET source_schema_version=:p,source_table=:t WHERE account_id=:a"),
                  {"p": F04_SCHEMA, "t": job.source_table(vendor, PROJECT), "a": ACCOUNT})
    with pytest.raises(ValueError):
        run(engine, vendor)


def test_cli_requires_explicit_scope_and_wires_both_routes(monkeypatch, capsys):
    for args in (["sync-warehouse-billing-summary"], ["sync-warehouse-billing-summary", "--vendor", "aws"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(args)
    settings = load_settings({"COST_INSIGHT_DB_URL": "sqlite://", "COST_INSIGHT_BILLING_BQ_PROJECT": PROJECT,
                              "COST_INSIGHT_BILLING_BQ_LOCATION": "us-west1"})
    monkeypatch.setattr(cli, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    engine, client = Mock(), Mock()
    monkeypatch.setattr(cli, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_billing_bigquery_client", Mock(return_value=client))
    runner = Mock(return_value={"percent": 0, "dry_run": True})
    monkeypatch.setattr(cli, "run_sync_warehouse_billing_summary", runner)
    for vendor in ("aws", "azure"):
        assert cli.main(["sync-warehouse-billing-summary", "--vendor", vendor,
                         "--account-id", ACCOUNT, "--account-id", "account-b", "--dry-run"]) == 0
        kw = runner.call_args.kwargs
        assert kw["vendor"] == vendor and kw["account_ids"] == [ACCOUNT, "account-b"]
        assert kw["client"] is client and kw["project"] == PROJECT and kw["dry_run"] is True
    assert engine.dispose.call_count == 2
    assert cli.main(["sync-warehouse-billing-summary", "--vendor", "aws", "--account-id", F04_ACCOUNT,
                     "--start-date", "2026-10-27", "--end-date", "2026-10-31", "--dry-run"]) == 0
    assert runner.call_args.kwargs["start_date"] == date(2026, 10, 27)
    assert runner.call_args.kwargs["end_date"] == date(2026, 10, 31)
    for bounds in (["--start-date", "2026-10-27"],
                   ["--start-date", "2026-10-27", "--end-date", "2026-11-01"],
                   ["--start-date", "2026-10-31", "--end-date", "2026-10-27"]):
        with pytest.raises(ValueError):
            cli.main(["sync-warehouse-billing-summary", "--vendor", "aws",
                      "--account-id", F04_ACCOUNT, "--dry-run", *bounds])
    assert runner.call_count == 3
    assert '"dry_run": true' in capsys.readouterr().out


@pytest.mark.parametrize("table,assignment", [
    ("cost_attribution_daily", "net_cost=99"),
    ("cost_resource_serving_daily", "effective_cost=99"),
    ("cost_resource_serving_daily", "currency='EUR'"),
    ("cost_resource_serving_daily", "materialization_version='obsolete'"),
    ("cost_resource_serving_publication", "active_materialization_version=NULL"),
    ("cost_resource_serving_publication", "currency='EUR'"),
    ("cost_resource_serving_publication", "total_list_cost=99"),
])
def test_publication_must_conserve_four_fields_and_resolve_active_version(
        engine, vendor, monkeypatch, table, assignment):
    def broken_publish(*args, **kwargs):
        publish(*args, **kwargs)
        with engine.begin() as c:
            c.execute(text(f"UPDATE {table} SET {assignment}"))
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", broken_publish)
    with pytest.raises(ValueError):
        run(engine, vendor)
    assert state(engine, vendor).last_status == "failed"
    assert not state(engine, vendor).watermark["completed_account_days"]


def test_changed_local_facts_after_preflight_stop_before_replacement(engine, vendor, monkeypatch):
    checkpoint = job._checkpoint
    def changed(engine, name, progress, *args):
        checkpoint(engine, name, progress, *args)
        if not args and progress.get("current_account_day") == f"{ACCOUNT}/{START}":
            write_summary_rows(engine, [normalized(vendor, day=START)], dry_run=False)
    monkeypatch.setattr(job, "_checkpoint", changed)
    replacement = Mock()
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    with pytest.raises(ValueError, match="identity count"):
        run(engine, vendor)
    replacement.assert_not_called()


def test_empty_partition_day_cannot_remove_existing_month_rollover_data(engine, vendor, monkeypatch):
    w = window(vendor, start=date(2026, 10, 29), end=date(2026, 11, 2))
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(return_value=w))
    write_summary_rows(engine, [normalized(vendor, day=date(2026, 10, 28)),
        normalized(vendor, day=date(2026, 11, 1), export_partition_date=date(2026, 10, 1))], dry_run=False)
    with pytest.raises(ValueError, match="Empty queried partition"):
        run(engine, vendor)


@pytest.mark.parametrize("accounts", [[], [""], [" account-a"], [ACCOUNT, ACCOUNT]])
def test_invalid_explicit_account_scope_never_acquires_state(engine, vendor, accounts):
    with pytest.raises(ValueError, match="account IDs"):
        job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor=vendor,
            account_ids=accounts, project=PROJECT, tcms_allocation_table=RULES)
    assert state(engine, vendor) is None


def test_production_writes_refuse_outside_gke(monkeypatch):
    from types import SimpleNamespace
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    monkeypatch.setattr(job.Path, "is_file", lambda path: False)
    with pytest.raises(ValueError, match="inside GKE"):
        run(engine, "aws")


def test_cli_requires_billing_project_and_location(monkeypatch):
    settings = load_settings({"COST_INSIGHT_DB_URL": "sqlite://"})
    monkeypatch.setattr(cli, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    with pytest.raises(ValueError, match="project and location"):
        cli.main(["sync-warehouse-billing-summary", "--vendor", "aws", "--account-id", ACCOUNT, "--dry-run"])


def test_each_write_has_one_snapshot_guard_and_one_post_write_scope_audit(engine, vendor, monkeypatch):
    snapshot = Mock()
    scopes = Mock(wraps=job._verify_scopes)
    replacement = job.replace_summary_partition_usage_dates
    checks_at_write = []
    def replace(*args, **kw):
        checks_at_write.append(snapshot.call_count)
        return replacement(*args, **kw)
    monkeypatch.setattr(job, "assert_snapshot", snapshot)
    monkeypatch.setattr(job, "_verify_scopes", scopes)
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replace)
    assert run(engine, vendor)["percent"] == 100
    assert checks_at_write == [2, 3, 4, 5, 6]  # Joint preflight, then exactly one guard per write.
    assert sum(c.kwargs.get("before", False) for c in scopes.call_args_list) == 5
    assert len(scopes.call_args_list) == 10  # Pre-write identity guard + post-write audit, no third scan.


def test_source_change_still_stops_later_writes_and_keeps_failure_latch(engine, vendor, monkeypatch):
    monkeypatch.setattr(job, "assert_snapshot", Mock(side_effect=[None, None, ValueError("snapshot changed")]))
    with pytest.raises(ValueError, match="snapshot changed"):
        run(engine, vendor)
    failed = state(engine, vendor)
    assert failed.last_status == "failed"
    assert failed.watermark["completed_account_days"] == [f"{ACCOUNT}/{START}"]
    assert failed.watermark["current_account_day"] == f"{ACCOUNT}/{START + timedelta(days=1)}"
    monkeypatch.setattr(job, "assert_snapshot", Mock())
    publisher = Mock(side_effect=publish)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publisher)
    result = run(engine, vendor)
    assert result["recovered_from_run_id"] == failed.watermark["run_id"]
    assert publisher.call_count == 4
    assert [c.kwargs["start_date"] for c in publisher.call_args_list] == list(job._dates(START + timedelta(days=1), END))


def fail_mid_window(engine, vendor, monkeypatch):
    def fail(engine, **kw):
        if kw["start_date"] == START + timedelta(days=2):
            raise RuntimeError("crashed after summary commit")
        publish(engine, **kw)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", fail)
    with pytest.raises(RuntimeError, match="crashed"):
        run(engine, vendor)
    return state(engine, vendor)


@pytest.mark.parametrize("legacy_profile", [False, True])
def test_failed_run_preserves_old_window_and_replays_only_unfinished_days(engine, vendor, monkeypatch, legacy_profile):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    if legacy_profile:
        # Real ordinary workers before the F04 cutover omitted source_profile.
        old = {k: v for k, v in failed.watermark.items() if k not in ("source_profile", "pod_uid", "pod_namespace")}
        with engine.begin() as c:
            state_store.mark_job_failed(c, job_name(vendor), old, "old worker failure")
        failed = state(engine, vendor)
    assert failed.watermark["completed_account_days"] == [f"{ACCOUNT}/{START}", f"{ACCOUNT}/{START + timedelta(days=1)}"]
    fetches = []
    def fetch(client, **kw):
        fetches.append(kw)
        # An ordinary discovery would now lose every pending day of the old run.
        first, last = kw.get("recovery_window") or (END + timedelta(days=10), END + timedelta(days=14))
        return window(vendor, start=first, end=last)
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    publisher = Mock(side_effect=publish)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publisher)
    replacement = Mock(wraps=job.replace_summary_partition_usage_dates)
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    result = run(engine, vendor)
    assert fetches[0]["recovery_window"] == (START, END)
    assert result["windows"] == failed.watermark["windows"]
    assert result["percent"] == 100 and len(result["completed_account_days"]) == 5
    assert [c.kwargs["start_date"] for c in publisher.call_args_list] == list(job._dates(START + timedelta(days=2), END))
    assert replacement.call_count == 3  # Whole pending day, even where summary already committed.
    with engine.connect() as c:
        for day in job._dates(START, END):
            total = job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, day)
            assert total["n"] == 1 and total["net_cost"] == 8
            job._verify_publication(c, vendor, ACCOUNT, day)
    with pytest.raises(ValueError, match="ownership"):
        job._checkpoint(engine, job_name(vendor), failed.watermark)


@pytest.mark.parametrize("running", [False, True])
def test_dry_run_does_not_reclaim_or_write_valid_recovery_state(engine, vendor, monkeypatch, running):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    if running:
        with engine.begin() as c:
            state_store.mark_job_started(c, job_name(vendor), failed.watermark)
    before = state(engine, vendor)
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda c, cur, sql, p, ctx, many: statements.append(sql.strip().upper()))
    guard, publisher = Mock(), Mock()
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publisher)
    run(engine, vendor, dry_run=True)
    assert state(engine, vendor) == before
    assert statements and all(s.startswith("SELECT") for s in statements)
    guard.assert_not_called()
    publisher.assert_not_called()


def test_preflight_failure_keeps_discovered_window_before_any_fact_write(engine, vendor, monkeypatch):
    normalize = job._normalize
    monkeypatch.setattr(job, "_normalize", Mock(side_effect=RuntimeError("preflight crash")))
    with pytest.raises(RuntimeError, match="preflight crash"):
        run(engine, vendor)
    old = state(engine, vendor)
    assert old.watermark["windows"] == [{"account_id": ACCOUNT, "start": str(START), "end": str(END)}]
    assert old.watermark["phase"] == "preflight" and not old.watermark["completed_account_days"]
    monkeypatch.setattr(job, "_normalize", normalize)
    def fetch(client, **kw):
        assert kw["recovery_window"] == (START, END)
        return window(vendor)
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    assert run(engine, vendor)["percent"] == 100


@pytest.mark.parametrize("table,assignment", [
    ("cost_attribution_daily", "net_cost=99"),
    ("cost_resource_serving_publication", "active_materialization_version='missing'"),
])
def test_completed_days_are_verified_before_skipping_or_replacing_any_pending_day(
        engine, vendor, monkeypatch, table, assignment):
    fail_mid_window(engine, vendor, monkeypatch)
    with engine.begin() as c:
        c.execute(text(f"UPDATE {table} SET {assignment} WHERE usage_date=:day"), {"day": START})
    replacement = Mock()
    monkeypatch.setattr(job, "replace_summary_partition_usage_dates", replacement)
    with pytest.raises(ValueError, match="summary -> attribution|Published native version"):
        run(engine, vendor)
    replacement.assert_not_called()


def test_running_owner_requires_proof_and_rechecks_state_after_pod_lookup(engine, vendor, monkeypatch):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    name = job_name(vendor)
    with engine.begin() as c:
        state_store.mark_job_started(c, name, failed.watermark)
    old = state(engine, vendor)
    guard = Mock(side_effect=ValueError("Pod is alive or unknown"))
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="running"):
        run(engine, vendor)
    assert state(engine, vendor) == old
    fetch.assert_not_called()

    def replaced_owner(progress):
        # Simulate a second contender winning while our Kubernetes request is in flight.
        with engine.begin() as c:
            state_store.mark_job_started(c, name, {**progress, "run_id": "another-worker"})
    guard.side_effect = replaced_owner
    with pytest.raises(ValueError, match="state changed"):
        run(engine, vendor)
    assert state(engine, vendor).watermark["run_id"] == "another-worker"
    fetch.assert_not_called()
    with pytest.raises(ValueError, match="ownership"):
        job._checkpoint(engine, name, old.watermark)
    assert state(engine, vendor).watermark["run_id"] == "another-worker"


def test_dead_running_owner_is_resumed_and_records_new_pod_identity(engine, vendor, monkeypatch):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    with engine.begin() as c:
        state_store.mark_job_started(c, job_name(vendor), failed.watermark)
    guard = Mock()
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish)
    for env, value in (("POD_NAME", "new-pod"), ("POD_UID", "new-uid"), ("POD_NAMESPACE", "apps")):
        monkeypatch.setenv(env, value)
    result = run(engine, vendor)
    guard.assert_called_once_with(failed.watermark)
    assert result["pod_uid"] == "new-uid" and result["pod_namespace"] == "apps"
    assert result["run_id"] != failed.watermark["run_id"] and result["percent"] == 100


@pytest.mark.parametrize("change", [
    {"accounts": [DORMANT]}, {"project": "another-project"}, {"source_profile": "custom"},
    {"completed_account_days": [f"{ACCOUNT}/2026-01-01"]},
    {"windows": []}, {"accounts": None}, {"run_id": []},
    {"completed_account_days": [{}]}, {"current_account_day": {}}, {"phase": "unknown"},
])
def test_recovery_refuses_changed_or_corrupt_persisted_scope(engine, vendor, monkeypatch, change):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    with engine.begin() as c:
        state_store.mark_job_failed(c, job_name(vendor), {**failed.watermark, **change}, "old")
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError):
        run(engine, vendor)
    fetch.assert_not_called()


@pytest.mark.parametrize("bad_window", [
    {"account_id": ACCOUNT}, "account-a/2026-11-20", 12345, None,
    {"account_id": ACCOUNT, "start": None, "end": str(END)},
    {"account_id": [], "start": str(START), "end": str(END)},
    {"account_id": ACCOUNT, "start": "not-a-date", "end": str(END)},
])
def test_malformed_recovery_window_is_actionable_and_keeps_checkpoint(
        engine, vendor, monkeypatch, bad_window):
    failed = fail_mid_window(engine, vendor, monkeypatch)
    with engine.begin() as c:
        state_store.mark_job_failed(c, job_name(vendor),
            {**failed.watermark, "windows": [bad_window]}, "original write failure")
    before = state(engine, vendor)
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="Malformed warehouse recovery window; inspect before recovery"):
        run(engine, vendor)
    assert state(engine, vendor) == before
    fetch.assert_not_called()


def test_empty_completions_do_not_allow_scope_reset_after_summary_commit(engine, vendor, monkeypatch):
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary",
                        Mock(side_effect=RuntimeError("crash after first summary commit")))
    with pytest.raises(RuntimeError, match="first summary commit"):
        run(engine, vendor)
    before = state(engine, vendor)
    assert before.watermark["completed_account_days"] == []
    with engine.connect() as c:
        assert job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, START)["n"] == 1
    fetch = Mock(side_effect=lambda client, **kw: window(vendor, kw["account_id"]))
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="original account scope|first summary commit"):
        job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor=vendor,
            account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES)
    if vendor == "aws":
        # The account keeps its pending window even when the requested sibling set changes.
        assert state(engine, vendor).watermark["windows"] == before.watermark["windows"]
        assert fetch.call_args_list[0].kwargs["recovery_window"] == (START, END)
    else:
        assert state(engine, vendor) == before
        fetch.assert_not_called()


def test_two_failed_state_contenders_cannot_both_acquire(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    with db.begin() as c:
        c.execute(text('''CREATE TABLE cost_job_state(job_name TEXT PRIMARY KEY,watermark_json TEXT,
            last_started_at TEXT,last_succeeded_at TEXT,last_status TEXT,last_error TEXT,updated_at TEXT)'''))
        previous = {"run_id": "old", "vendor": "azure", "project": PROJECT, "source_profile": "ordinary",
                    "accounts": [ACCOUNT], "windows": [{"account_id": ACCOUNT, "start": str(START), "end": str(END)}],
                    "completed_account_days": [], "phase": "write"}
        state_store.mark_job_failed(c, "worker", previous, "crashed")
    barrier = Barrier(2)
    resume = job._resume_progress
    def synchronized(*args):
        resume(*args)
        barrier.wait(timeout=5)
    monkeypatch.setattr(job, "_resume_progress", synchronized)
    def acquire(run_id):
        progress = {**previous, "run_id": run_id, "accounts": (ACCOUNT,)}
        try:
            job._acquire(db, "worker", progress)
            return True
        except ValueError as exc:
            assert "state changed" in str(exc)
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(acquire, ("one", "two"))) == [False, True]
    finally:
        db.dispose()


def test_all_days_checkpointed_before_terminal_failure_need_no_republication(engine, vendor, monkeypatch):
    checkpoint = job._checkpoint
    def fail_success(*args, **kwargs):
        if len(args) > 3 and args[3] == "succeeded":
            raise RuntimeError("crashed before terminal checkpoint")
        return checkpoint(*args, **kwargs)
    monkeypatch.setattr(job, "_checkpoint", fail_success)
    with pytest.raises(RuntimeError, match="terminal checkpoint"):
        run(engine, vendor)
    assert len(state(engine, vendor).watermark["completed_account_days"]) == 5
    monkeypatch.setattr(job, "_checkpoint", checkpoint)
    publisher = Mock()
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publisher)
    assert run(engine, vendor)["percent"] == 100
    publisher.assert_not_called()
    assert state(engine, vendor).last_status == "succeeded"


def test_f04_short_historical_failure_resumes_without_explicit_cli_dates(f04_engine, monkeypatch):
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", Mock(side_effect=RuntimeError("crashed")))
    with pytest.raises(RuntimeError, match="crashed"):
        run_f04(f04_engine, start_date=START, end_date=START)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish)
    result = run_f04(f04_engine)
    assert result["percent"] == 100
    assert result["completed_account_days"] == [f"{F04_ACCOUNT}/{START}"]


def test_f04_preflight_crash_preserves_explicit_window_and_requires_profile(f04_engine, monkeypatch):
    fetch = job.fetch_warehouse_window
    monkeypatch.setattr(job, "fetch_warehouse_window", Mock(side_effect=RuntimeError("preflight crash")))
    with pytest.raises(RuntimeError, match="preflight crash"):
        run_f04(f04_engine, start_date=START, end_date=START)
    with f04_engine.connect() as c:
        previous = state_store.get_job_state(c, "sync_aws_f04_warehouse_daily")
    assert previous.watermark["windows"] == [{"account_id": F04_ACCOUNT, "start": str(START), "end": str(START)}]
    legacy = {k: v for k, v in previous.watermark.items() if k != "source_profile"}
    with f04_engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_f04_warehouse_daily", legacy, "missing profile")
    with pytest.raises(ValueError, match="source_profile"):
        run_f04(f04_engine)
    job.fetch_warehouse_window.assert_called_once()  # No new lookup for ambiguous F04 checkpoint.
    with f04_engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_f04_warehouse_daily", previous.watermark, "old failure")
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    result = run_f04(f04_engine)
    assert result["completed_account_days"] == [f"{F04_ACCOUNT}/{START}"]
    assert result["percent"] == 100


@pytest.fixture
def ordinary_aws_engine(engine, vendor):
    if vendor != "aws":
        pytest.skip("Ordinary AWS account isolation")
    return engine


def run_accounts(engine, **extra):
    return job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor="aws",
        account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES, **extra)


def legacy_checkpoint():
    return {"run_id": "legacy-run", "vendor": "aws", "project": PROJECT,
            "accounts": [ACCOUNT, "account-b"], "pod_name": "legacy-pod",
            "windows": [{"account_id": a, "start": str(START), "end": str(END)}
                        for a in (ACCOUNT, "account-b")],
            "phase": "write", "current_account_day": f"{ACCOUNT}/{START}",
            "completed_account_days": []}


def test_all_ordinary_accounts_complete_with_distinct_ownership(ordinary_aws_engine):
    engine = ordinary_aws_engine
    results = run_accounts(engine)["account_results"]
    assert all(result["status"] == "succeeded" for result in results.values())
    states = [state(engine, "aws", account) for account in (ACCOUNT, "account-b")]
    assert all(s.last_status == "succeeded" and len(s.watermark["completed_account_days"]) == 5 for s in states)
    assert states[0].watermark["run_id"] != states[1].watermark["run_id"]


@pytest.mark.parametrize("failure", ["fetch", "registry", "currency", "backlog", "unverified_latch"])
def test_account_local_preflight_and_failed_latch_do_not_block_sibling(ordinary_aws_engine, monkeypatch, failure):
    engine = ordinary_aws_engine
    before = None
    if failure == "unverified_latch":
        with engine.begin() as c:
            state_store.mark_job_failed(c, job_name("aws"), {"run_id": "unverified"}, "inspect me")
        before = state(engine, "aws")
    elif failure == "registry":
        with engine.begin() as c:
            c.execute(text("UPDATE cost_sources SET is_active=0 WHERE account_id=:a"), {"a": ACCOUNT})
    elif failure == "backlog":
        with engine.begin() as c:
            c.execute(text(f"DELETE FROM {job.SUMMARY} WHERE account_id=:a"), {"a": ACCOUNT})
    def fetch(client, **kw):
        account = kw["account_id"]
        if account == ACCOUNT and failure == "fetch":
            raise RuntimeError("source unavailable for A")
        w = window("aws", account)
        if account == ACCOUNT and failure == "currency":
            return WarehouseWindow(w.start, w.end, ({**w.rows[0], "currency": "EUR"}, *w.rows[1:]),
                                   w.controls, w.snapshots)
        return w
    fetcher = Mock(side_effect=fetch)
    monkeypatch.setattr(job, "fetch_warehouse_window", fetcher)
    with pytest.raises(job.WarehouseAccountFailures) as raised:
        run_accounts(engine)
    results = raised.value.summary["account_results"]
    assert results[ACCOUNT]["status"] == "failed" and results[ACCOUNT]["error"]
    assert results["account-b"]["status"] == "succeeded"
    assert state(engine, "aws", "account-b").last_status == "succeeded"
    if before:
        assert state(engine, "aws") == before  # No automatic reset of unverified state.
        assert [c.kwargs["account_id"] for c in fetcher.call_args_list] == ["account-b"]
    else:
        assert state(engine, "aws").last_status == "failed"
    with engine.connect() as c:
        for day in job._dates(START, END):
            job._verify_publication(c, "aws", "account-b", day)


def test_account_keys_can_be_running_independently_but_same_account_is_exclusive(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    db = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}")
    with db.begin() as c:
        c.execute(text('''CREATE TABLE cost_job_state(job_name TEXT PRIMARY KEY,watermark_json TEXT,
            last_started_at TEXT,last_succeeded_at TEXT,last_status TEXT,last_error TEXT,updated_at TEXT)'''))
    def acquire(account):
        progress = {**legacy_checkpoint(), "run_id": f"run-{account}", "source_profile": "ordinary",
                    "accounts": (account,), "windows": [], "completed_account_days": [], "phase": "preflight"}
        job._acquire(db, job_name("aws", account), progress)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(acquire, (ACCOUNT, "account-b")))
        before = [state(db, "aws", account) for account in (ACCOUNT, "account-b")]
        assert [s.last_status for s in before] == ["running", "running"]
        monkeypatch.setattr(job, "assert_owner_stopped", Mock(side_effect=ValueError("live owner")))
        with pytest.raises(ValueError, match="running"):
            acquire(ACCOUNT)
        assert [state(db, "aws", account) for account in (ACCOUNT, "account-b")] == before
    finally:
        db.dispose()


def test_two_legacy_migrations_cannot_overwrite_scoped_checkpoints(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with db.begin() as c:
        c.execute(text('''CREATE TABLE cost_job_state(job_name TEXT PRIMARY KEY,watermark_json TEXT,
            last_started_at TEXT,last_succeeded_at TEXT,last_status TEXT,last_error TEXT,updated_at TEXT)'''))
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", legacy_checkpoint(), "A failed")
    barrier = Barrier(2)
    resume = job._resume_progress
    def synchronized(*args):
        resume(*args)
        barrier.wait(timeout=5)
    monkeypatch.setattr(job, "_resume_progress", synchronized)
    def migrate(_):
        try:
            job._migrate_ordinary_aws_state(db, PROJECT)
            return True
        except ValueError as exc:
            assert "state changed" in str(exc)
            return False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(migrate, (1, 2))) == [False, True]
        with db.connect() as c:
            assert state_store.get_job_state(c, "sync_aws_warehouse_daily").last_status == "account_scoped"
            assert c.execute(text("SELECT COUNT(*) FROM cost_job_state")).scalar_one() == 3
        assert all(state(db, "aws", account).last_status == "failed" for account in (ACCOUNT, "account-b"))
    finally:
        db.dispose()


def test_live_account_owner_does_not_block_other_account(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    progress = {**legacy_checkpoint(), "accounts": (ACCOUNT,),
                "windows": [legacy_checkpoint()["windows"][0]], "source_profile": "ordinary"}
    job._migrate_ordinary_aws_state(engine, PROJECT)
    job._acquire(engine, job_name("aws"), progress)
    before = state(engine, "aws")
    guard = Mock(side_effect=ValueError("owner Pod is still alive"))
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    with pytest.raises(job.WarehouseAccountFailures, match="running"):
        run_accounts(engine)
    assert state(engine, "aws") == before
    assert state(engine, "aws", "account-b").last_status == "succeeded"
    guard.assert_called_once_with(before.watermark)


@pytest.mark.parametrize("running", [False, True])
def test_legacy_migration_preserves_pending_windows_and_completed_days(ordinary_aws_engine, monkeypatch, running):
    engine = ordinary_aws_engine
    write_summary_rows(engine, [normalized("aws", day=START)], dry_run=False)
    publish(engine, source=job.CostAttributionSource("aws", ACCOUNT), start_date=START,
            end_date=START, tcms_allocation_table=RULES)
    old = {**legacy_checkpoint(), "completed_account_days": [f"{ACCOUNT}/{START}"],
           "current_account_day": f"{ACCOUNT}/{START + timedelta(days=1)}"}
    with engine.begin() as c:
        if running:
            state_store.mark_job_started(c, "sync_aws_warehouse_daily", old)
        else:
            state_store.mark_job_failed(c, "sync_aws_warehouse_daily", old, "A crashed")
    guard = Mock()
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    fetches = []
    def fetch(client, **kw):
        fetches.append(kw)
        assert kw["recovery_window"] == (START, END)
        return window("aws", kw["account_id"])
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    def fail_a(engine, **kw):
        if kw["source"].account_id == ACCOUNT:
            raise RuntimeError("A still fails")
        publish(engine, **kw)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", fail_a)
    with pytest.raises(job.WarehouseAccountFailures, match="A still fails"):
        run_accounts(engine)
    assert len(fetches) == 2
    assert state(engine, "aws").watermark["completed_account_days"] == old["completed_account_days"]
    assert state(engine, "aws").watermark["windows"] == [old["windows"][0]]
    assert state(engine, "aws", "account-b").last_status == "succeeded"
    with engine.connect() as c:
        fence = state_store.get_job_state(c, "sync_aws_warehouse_daily")
    assert fence.last_status == "account_scoped" and fence.watermark == old
    # Even a PR954/old-key caller cannot acquire after migration.
    with pytest.raises(ValueError, match="account_scoped"):
        job._acquire(engine, "sync_aws_warehouse_daily", {**old, "accounts": tuple(old["accounts"])})
    assert guard.call_count == int(running)


def test_migration_requested_subset_keeps_unrequested_pending_account(ordinary_aws_engine):
    engine = ordinary_aws_engine
    old = legacy_checkpoint()
    with engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", old, "A failed")
    result = job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor="aws",
        account_ids=["account-b"], project=PROJECT, tcms_allocation_table=RULES)
    assert result["windows"] == [old["windows"][1]] and result["percent"] == 100
    pending = state(engine, "aws")
    assert pending.last_status == "failed" and pending.watermark["run_id"] == old["run_id"]
    assert pending.watermark["windows"] == [old["windows"][0]]
    assert "Interrupted legacy checkpoint" in pending.last_error


def test_overlapping_legacy_and_account_state_rolls_back_entire_migration(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    with engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", legacy_checkpoint(), "A failed")
        state_store.mark_job_failed(c, job_name("aws", "account-b"), {"run_id": "other"}, "other checkpoint")
        before = c.execute(text("SELECT * FROM cost_job_state ORDER BY job_name")).all()
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="overlap"):
        run_accounts(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT * FROM cost_job_state ORDER BY job_name")).all() == before
    fetch.assert_not_called()


@pytest.mark.parametrize("change", [
    {"phase": "preflight", "current_account_day": None}, {"current_account_day": None},
    {"current_account_day": "unknown/2026-10-25"}, {"windows": []},
    {"project": "wrong-project"}, {"accounts": ["account-b", ACCOUNT]},
])
def test_unattributed_or_corrupt_legacy_failure_is_globally_fail_closed(ordinary_aws_engine, monkeypatch, change):
    engine = ordinary_aws_engine
    with engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", {**legacy_checkpoint(), **change}, "unknown failure")
        before = state_store.get_job_state(c, "sync_aws_warehouse_daily")
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="inspect before recovery"):
        run_accounts(engine)
    with engine.connect() as c:
        assert state_store.get_job_state(c, "sync_aws_warehouse_daily") == before
        assert c.execute(text("SELECT COUNT(*) FROM cost_job_state")).scalar_one() == 1
    fetch.assert_not_called()


@pytest.mark.parametrize("changed_owner", [False, True])
def test_legacy_running_owner_proof_and_migration_cas_cannot_be_bypassed(ordinary_aws_engine, monkeypatch, changed_owner):
    engine = ordinary_aws_engine
    old = legacy_checkpoint()
    with engine.begin() as c:
        state_store.mark_job_started(c, "sync_aws_warehouse_daily", old)
    def guard(progress):
        if not changed_owner:
            raise ValueError("owner still alive or unprovable")
        with engine.begin() as c:
            state_store.mark_job_started(c, "sync_aws_warehouse_daily", {**progress, "run_id": "other-owner"})
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    fetch = Mock()
    monkeypatch.setattr(job, "fetch_warehouse_window", fetch)
    with pytest.raises(ValueError, match="running|state changed"):
        run_accounts(engine)
    with engine.connect() as c:
        assert state_store.get_job_state(c, "sync_aws_warehouse_daily").last_status == "running"
        assert c.execute(text("SELECT COUNT(*) FROM cost_job_state")).scalar_one() == 1
    fetch.assert_not_called()


def test_multi_account_dry_run_never_fences_or_reclaims_any_latch(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    with engine.begin() as c:
        state_store.mark_job_failed(c, "sync_aws_warehouse_daily", legacy_checkpoint(), "legacy failure")
        state_store.mark_job_failed(c, job_name("aws"), {"run_id": "unknown"}, "account failure")
        before = c.execute(text("SELECT * FROM cost_job_state ORDER BY job_name")).all()
    guard = Mock()
    monkeypatch.setattr(job, "assert_owner_stopped", guard)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql.strip().upper()))
    result = run_accounts(engine, dry_run=True)
    assert all(r["status"] == "dry_run" for r in result["account_results"].values())
    with engine.connect() as c:
        assert c.execute(text("SELECT * FROM cost_job_state ORDER BY job_name")).all() == before
    assert statements and all(s.startswith("SELECT") for s in statements)
    guard.assert_not_called()


def test_shared_source_change_stops_all_pending_accounts(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    monkeypatch.setattr(job, "assert_snapshot", Mock(side_effect=[None, None, job.WarehouseSnapshotError("shared snapshot changed")]))
    with pytest.raises(ValueError, match="shared snapshot changed"):
        run_accounts(engine)
    assert state(engine, "aws").watermark["completed_account_days"] == [f"{ACCOUNT}/{START}"]
    assert state(engine, "aws", "account-b").last_status == "failed"
    with engine.connect() as c:
        assert job._table_totals(c, job.SUMMARY, "aws", "account-b", START)["n"] == 0


def test_cli_reports_partial_failure_as_nonzero(ordinary_aws_engine, monkeypatch, capsys):
    import json

    engine = ordinary_aws_engine
    settings = load_settings({"COST_INSIGHT_DB_URL": "sqlite://", "COST_INSIGHT_BILLING_BQ_PROJECT": PROJECT,
                              "COST_INSIGHT_BILLING_BQ_LOCATION": "us-west1"})
    monkeypatch.setattr(cli, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_billing_bigquery_client", Mock(return_value=Mock()))
    monkeypatch.setattr(engine, "dispose", Mock())
    with engine.begin() as c:
        state_store.mark_job_failed(c, job_name("aws"), {"run_id": "unknown"}, "unverified")
    assert cli.main(["sync-warehouse-billing-summary", "--vendor", "aws",
                     "--account-id", ACCOUNT, "--account-id", "account-b"]) == 1
    results = json.loads(capsys.readouterr().out)["account_results"]
    assert results[ACCOUNT]["status"] == "failed" and results[ACCOUNT]["error"]
    assert results["account-b"]["status"] == "succeeded"
    engine.dispose.assert_called_once()


def real_warehouse_client(*, drift_at_metadata_call=None):
    from types import SimpleNamespace

    client = Mock()
    status = {"changed": False, "metadata_calls": 0, "cancel": False, "jobs": []}
    def metadata(*args, **kwargs):
        status["metadata_calls"] += 1
        if drift_at_metadata_call and status["metadata_calls"] >= drift_at_metadata_call:
            status["changed"] = True
        return SimpleNamespace(table_type="TABLE", expires=None, time_partitioning=None,
            modified=datetime(2026, 10, 31, tzinfo=UTC), num_rows=101 if status["changed"] else 100,
            etag="changed" if status["changed"] else "stable")
    def query(sql, *, job_config, **kwargs):
        query_job = Mock(total_bytes_processed=100, job_id="query")
        status["jobs"].append(query_job)
        if not job_config.dry_run:
            account = next(p.value for p in job_config.query_parameters if p.name == "account_id")
            w = window("aws", account)
            query_job.result.return_value = w.controls if "COUNT(*) AS n" in sql else w.rows
            if status["cancel"]:
                query_job.result.side_effect = KeyboardInterrupt("cancel source read")
        return query_job
    client.get_table.side_effect = metadata
    client.query.side_effect = query
    return client, status


def use_real_warehouse_reader(monkeypatch):
    from cost_insight.sources import warehouse_billing_export as source

    monkeypatch.setattr(job, "fetch_warehouse_window", source.fetch_warehouse_window)
    monkeypatch.setattr(job, "assert_snapshot", source.assert_snapshot)


def test_source_internal_snapshot_drift_aborts_all_accounts_before_writes(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    use_real_warehouse_reader(monkeypatch)
    # Initial snapshot, query pre-guard, then actual source post-read guard drifts.
    client, _ = real_warehouse_client(drift_at_metadata_call=3)
    with pytest.raises(job.WarehouseAccountFailures) as raised:
        job.run_sync_warehouse_billing_summary(engine, client=client, vendor="aws",
            account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES,
            today=END + timedelta(days=2))
    results = raised.value.summary["account_results"]
    assert all(r["status"] == "failed" and "snapshot" in r["error"] for r in results.values())
    assert {p.value for call in client.query.call_args_list
            for p in call.kwargs["job_config"].query_parameters if p.name == "account_id"} == {ACCOUNT}
    assert all(state(engine, "aws", a).last_status == "failed" for a in (ACCOUNT, "account-b"))
    with engine.connect() as c:
        assert c.execute(text(f"SELECT COUNT(*) FROM {job.SUMMARY} WHERE usage_date >= :start"),
                         {"start": START}).scalar_one() == 0


def test_real_source_snapshot_abort_dry_run_remains_select_only(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    use_real_warehouse_reader(monkeypatch)
    client, _ = real_warehouse_client(drift_at_metadata_call=3)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, p, ctx, many: statements.append(sql.strip().upper()))
    with pytest.raises(job.WarehouseAccountFailures) as raised:
        job.run_sync_warehouse_billing_summary(engine, client=client, vendor="aws",
            account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES,
            today=END + timedelta(days=2), dry_run=True)
    assert all(r["status"] == "failed" for r in raised.value.summary["account_results"].values())
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM cost_job_state")).scalar_one() == 0
    assert statements and all(s.startswith("SELECT") for s in statements)


def test_cli_shared_snapshot_abort_after_sibling_success_keeps_json_and_states(ordinary_aws_engine, monkeypatch, capsys):
    import json
    from functools import partial

    engine = ordinary_aws_engine
    use_real_warehouse_reader(monkeypatch)
    client, metadata = real_warehouse_client()
    def publish_then_drift(engine, **kwargs):
        publish(engine, **kwargs)
        if kwargs["source"].account_id == ACCOUNT and kwargs["start_date"] == END:
            metadata["changed"] = True  # Real writer guard for B now sees a changed shared table.
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish_then_drift)
    settings = load_settings({"COST_INSIGHT_DB_URL": "sqlite://", "COST_INSIGHT_BILLING_BQ_PROJECT": PROJECT,
                              "COST_INSIGHT_BILLING_BQ_LOCATION": "us-west1"})
    monkeypatch.setattr(cli, "get_settings", lambda **kw: settings)
    monkeypatch.setattr(cli, "configure_logging", Mock())
    monkeypatch.setattr(cli, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_billing_bigquery_client", Mock(return_value=client))
    monkeypatch.setattr(cli, "run_sync_warehouse_billing_summary", partial(
        job.run_sync_warehouse_billing_summary, today=END + timedelta(days=2)))
    monkeypatch.setattr(engine, "dispose", Mock())
    assert cli.main(["sync-warehouse-billing-summary", "--vendor", "aws",
                     "--account-id", ACCOUNT, "--account-id", "account-b"]) == 1
    results = json.loads(capsys.readouterr().out)["account_results"]
    assert results[ACCOUNT]["status"] == "succeeded" and results[ACCOUNT]["error"] is None
    assert results["account-b"]["status"] == "failed" and "snapshot" in results["account-b"]["error"]
    assert state(engine, "aws").last_status == "succeeded"
    assert state(engine, "aws", "account-b").last_status == "failed"
    with engine.connect() as c:
        assert job._table_totals(c, job.SUMMARY, "aws", "account-b", START)["n"] == 0
        for day in job._dates(START, END):
            job._verify_publication(c, "aws", ACCOUNT, day)


def test_source_cancellation_cleans_all_owners_and_propagates_base_exception(ordinary_aws_engine, monkeypatch):
    engine = ordinary_aws_engine
    use_real_warehouse_reader(monkeypatch)
    client, metadata = real_warehouse_client()
    metadata["cancel"] = True
    with pytest.raises(KeyboardInterrupt, match="cancel source read"):
        job.run_sync_warehouse_billing_summary(engine, client=client, vendor="aws",
            account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES,
            today=END + timedelta(days=2))
    assert all(state(engine, "aws", a).last_status == "failed" for a in (ACCOUNT, "account-b"))
    metadata["jobs"][-1].cancel.assert_called_once_with(timeout=30, retry=None)


def test_ordinary_aws_account_write_failure_does_not_stop_sibling(engine, vendor, monkeypatch):
    if vendor != "aws":
        pytest.skip("Ordinary AWS account isolation")
    def fail_one(engine, **kw):
        if kw["source"].account_id == ACCOUNT:
            raise RuntimeError("account-a publication failed")
        publish(engine, **kw)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", fail_one)
    with pytest.raises((ValueError, RuntimeError), match="account-a publication failed"):
        job.run_sync_warehouse_billing_summary(engine, client=Mock(), vendor=vendor,
            account_ids=[ACCOUNT, "account-b"], project=PROJECT, tcms_allocation_table=RULES)
    with engine.connect() as c:
        for day in job._dates(START, END):
            job._verify_publication(c, vendor, "account-b", day)
        failed = state_store.get_job_state(c, "sync_aws_warehouse_daily:aws:account-a")
        succeeded = state_store.get_job_state(c, "sync_aws_warehouse_daily:aws:account-b")
    assert failed.last_status == "failed" and "publication failed" in failed.last_error
    assert succeeded.last_status == "succeeded"


def test_recovery_after_transport_fault_still_replaces_not_adds(engine, vendor, monkeypatch):
    from pymysql.err import OperationalError

    from cost_insight.common import db_retry

    fail_mid_window(engine, vendor, monkeypatch)
    monkeypatch.setattr(job, "run_refresh_cost_attribution_from_summary", publish)
    monkeypatch.setattr(db_retry.time, "sleep", Mock())
    failed = False
    def fault(connection, cursor, statement, params, context, many):
        nonlocal failed
        if not failed and statement.replace("`", "").lstrip().startswith(f"INSERT INTO {job.SUMMARY}"):
            failed = True
            raise OperationalError(2013, "Lost connection during resumed insert")
    event.listen(engine, "before_cursor_execute", fault)
    assert run(engine, vendor)["percent"] == 100
    db_retry.time.sleep.assert_called_once()
    with engine.connect() as c:
        for day in job._dates(START, END):
            total = job._table_totals(c, job.SUMMARY, vendor, ACCOUNT, day)
            assert total["n"] == 1 and total["net_cost"] == 8
            job._verify_publication(c, vendor, ACCOUNT, day)
