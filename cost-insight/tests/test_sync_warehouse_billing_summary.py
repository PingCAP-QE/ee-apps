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


def state(engine, vendor):
    with engine.connect() as c:
        return state_store.get_job_state(c, f"sync_{vendor}_warehouse_daily")


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
        state_store.mark_job_failed(c, f"sync_{vendor}_warehouse_daily", {"run_id": "old"}, "existing failure")
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
    with pytest.raises(ValueError, match="inspect before recovery"):
        run(engine, vendor)
    assert state(engine, vendor) == failed


def test_running_legacy_operational_key_cannot_be_bypassed(engine, vendor):
    with engine.begin() as c:
        state_store.mark_job_started(c, f"sync_{vendor}_warehouse_daily", {"run_id": "legacy-configmap-worker"})
    with pytest.raises(ValueError, match="running"):
        run(engine, vendor)
    assert state(engine, vendor).watermark["run_id"] == "legacy-configmap-worker"


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
    with pytest.raises(ValueError, match="inspect before recovery"):
        run(engine, vendor)
    assert state(engine, vendor) == failed
