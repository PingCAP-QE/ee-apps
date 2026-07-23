import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.refresh_attribution_daily import (
    CostAttributionSource,
    JOB_NAME,
    SUMMARY_JOB_NAME,
    _INSERT_ATTRIBUTION_DAILY,
    _INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,
    _quote_table_identifier,
    _summary_insert_statements,
    normalized_identity_sql,
    _positive_rowcount,
    _watermark,
    run_refresh_cost_attribution_daily,
    run_refresh_cost_attribution_from_summary,
)

SOURCE = CostAttributionSource(vendor="gcp", account_id="pingcap-testing-account")


def _sqlite_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        _register_mysqlish_sqlite_functions(connection)
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


def _register_mysqlish_sqlite_functions(connection) -> None:
    raw_connection = connection.connection.driver_connection
    json_null = "__JSON_NULL__"

    def concat(*values):
        return "".join("" if value is None else str(value) for value in values)

    def concat_ws(separator, *values):
        return str(separator).join(str(value) for value in values if value is not None)

    def date_format(value, pattern):
        if value is None:
            return None
        if pattern == "%Y-%m-%d":
            return str(value)[:10]
        return str(value)

    def sha2(value, bits):
        if value is None:
            value = ""
        if int(bits) != 256:
            raise ValueError("test SHA2 only supports 256 bits")
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def substring_index(value, delimiter, count):
        if value is None:
            return ""
        parts = str(value).split(str(delimiter))
        count = int(count)
        if count >= 0:
            return str(delimiter).join(parts[:count])
        return str(delimiter).join(parts[count:])

    def json_unquote(value):
        if value == json_null:
            return None
        return value

    def json_extract(value, path):
        if value is None:
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or not path.startswith("$."):
            return None
        key = path[2:]
        if key not in parsed:
            return None
        extracted = parsed[key]
        if extracted is None:
            return json_null
        if isinstance(extracted, (dict, list)):
            return json.dumps(extracted, sort_keys=True, separators=(",", ":"))
        return extracted

    def json_type(value):
        if value is None:
            return None
        if value == json_null:
            return "NULL"
        return "OBJECT" if isinstance(value, str) and value.startswith("{") else "STRING"

    def json_remove(value, *paths):
        if value is None:
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            return value
        for path in paths:
            if path.startswith("$."):
                parsed.pop(path[2:], None)
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))

    def json_contains(target, candidate):
        if target is None or candidate is None:
            return 0
        target_json = json.loads(target)
        candidate_json = json.loads(candidate)
        return int(
            all(
                key in target_json and target_json[key] == value
                for key, value in candidate_json.items()
            )
        )

    def json_length(value):
        if value is None:
            return None
        parsed = json.loads(value)
        return len(parsed) if isinstance(parsed, dict) else 0

    raw_connection.create_function("CONCAT", -1, concat)
    raw_connection.create_function("CONCAT_WS", -1, concat_ws)
    raw_connection.create_function("DATE_FORMAT", 2, date_format)
    raw_connection.create_function("JSON_CONTAINS", 2, json_contains)
    raw_connection.create_function("JSON_EXTRACT", 2, json_extract)
    raw_connection.create_function("JSON_LENGTH", 1, json_length)
    raw_connection.create_function("JSON_REMOVE", -1, json_remove)
    raw_connection.create_function("JSON_TYPE", 1, json_type)
    raw_connection.create_function("JSON_UNQUOTE", 1, json_unquote)
    raw_connection.create_function("SHA2", 2, sha2)
    raw_connection.create_function("SUBSTRING_INDEX", 3, substring_index)


def test_watermark_formats_dates() -> None:
    assert _watermark(
        vendor="gcp",
        account_id="pingcap-testing-account",
        start_date=date(2026, 5, 9),
        end_date=date(2026, 5, 17),
    ) == {
        "vendor": "gcp",
        "account_id": "pingcap-testing-account",
        "start_date": "2026-05-09",
        "end_date": "2026-05-17",
    }


def test_positive_rowcount_normalizes_unknown_values() -> None:
    assert _positive_rowcount(None) == 0
    assert _positive_rowcount(-1) == 0
    assert _positive_rowcount(3) == 3


def test_normalized_identity_sql_replaces_label_unsafe_characters() -> None:
    sql = normalized_identity_sql("employee.email")

    assert "LOWER(COALESCE(employee.email, ''))" in sql
    assert "SUBSTRING_INDEX" in sql
    assert "'@'" in sql
    assert "'-'" in sql
    assert "'.'" in sql
    assert "'_'" in sql
    assert "' '" in sql


def test_run_refresh_attribution_dry_run_counts_raw_rows() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_raw_details (
                      usage_date DATE NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_raw_details (usage_date, vendor, account_id)
                    VALUES
                      ('2026-05-09', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'aws', '123456789012')
                    """
                )
            )

        summary = run_refresh_cost_attribution_daily(
            engine,
            source=SOURCE,
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
            dry_run=True,
        )

        assert summary.raw_rows == 2
        assert summary.rows_deleted == 0
        assert summary.rows_inserted == 0
        assert summary.dry_run is True
        with engine.begin() as connection:
            assert (
                state_store.get_job_state(
                    connection,
                    source_job_name(JOB_NAME, vendor=SOURCE.vendor, account_id=SOURCE.account_id),
                )
                is None
            )
    finally:
        engine.dispose()


def test_run_refresh_attribution_from_summary_dry_run_counts_summary_rows() -> None:
    engine = _sqlite_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_bq_export_summary_daily (
                      usage_date DATE NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (usage_date, vendor, account_id)
                    VALUES
                      ('2026-05-09', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'gcp', 'pingcap-testing-account'),
                      ('2026-05-10', 'aws', '123456789012')
                    """
                )
            )

        summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=SOURCE,
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
            dry_run=True,
        )

        assert summary.summary_rows == 2
        assert summary.rows_deleted == 0
        assert summary.rows_inserted == 0
        assert summary.dry_run is True
        with engine.begin() as connection:
            assert (
                state_store.get_job_state(
                    connection,
                    source_job_name(
                        SUMMARY_JOB_NAME,
                        vendor=SOURCE.vendor,
                        account_id=SOURCE.account_id,
                    ),
                )
                is None
            )
    finally:
        engine.dispose()


def test_run_refresh_attribution_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    executed = []

    def fake_execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "DELETE FROM cost_attribution_daily" in sql:
            executed.append(("delete", params))

            class Result:
                rowcount = 4

            return Result()
        if "INSERT INTO cost_attribution_daily" in sql:
            executed.append(("insert", params))

            class Result:
                rowcount = 7

            return Result()
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        summary = run_refresh_cost_attribution_daily(
            engine,
            source=SOURCE,
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
        )

        assert summary.rows_deleted == 4
        assert summary.rows_inserted == 7
        assert [kind for kind, _params in executed] == ["delete", "insert"]
        assert executed[0][1]["account_id"] == "pingcap-testing-account"
        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(JOB_NAME, vendor=SOURCE.vendor, account_id=SOURCE.account_id),
            )
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_refresh_attribution_from_summary_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    executed = []

    def fake_execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "DELETE FROM cost_attribution_daily" in sql:
            executed.append(("delete", params))

            class Result:
                rowcount = 2

            return Result()
        if "FROM cost_bq_export_summary_daily summary" in sql:
            executed.append(("insert-summary", params))

            class Result:
                rowcount = 5

            return Result()
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=SOURCE,
            start_date=date(2026, 5, 9),
            end_date=date(2026, 5, 10),
        )

        assert summary.rows_deleted == 2
        assert summary.rows_inserted == 5
        assert [kind for kind, _params in executed] == ["delete", "insert-summary"]
        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(
                    SUMMARY_JOB_NAME,
                    vendor=SOURCE.vendor,
                    account_id=SOURCE.account_id,
                ),
            )
        assert state is not None
        assert state.last_status == "succeeded"
    finally:
        engine.dispose()


def test_run_refresh_attribution_marks_failure(monkeypatch) -> None:
    engine = _sqlite_engine()

    def fake_execute(self, statement, params=None, *args, **kwargs):
        if "DELETE FROM cost_attribution_daily" in str(statement):
            raise RuntimeError("delete failed")
        return original_execute(self, statement, params, *args, **kwargs)

    original_execute = Connection.execute
    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)

    try:
        with pytest.raises(RuntimeError, match="delete failed"):
            run_refresh_cost_attribution_daily(
                engine,
                source=SOURCE,
                start_date=date(2026, 5, 9),
                end_date=date(2026, 5, 10),
            )

        with engine.begin() as connection:
            state = state_store.get_job_state(
                connection,
                source_job_name(JOB_NAME, vendor=SOURCE.vendor, account_id=SOURCE.account_id),
            )
        assert state is not None
        assert state.last_status == "failed"
        assert "RuntimeError" in (state.last_error or "")
    finally:
        engine.dispose()


def test_run_refresh_attribution_rejects_invalid_range() -> None:
    engine = _sqlite_engine()
    try:
        with pytest.raises(ValueError, match="start_date"):
            run_refresh_cost_attribution_daily(
                engine,
                source=SOURCE,
                start_date=date(2026, 5, 10),
                end_date=date(2026, 5, 9),
            )
    finally:
        engine.dispose()


def test_run_refresh_aws_summary_with_tcms_preserves_author_and_allocates_shared() -> None:
    engine = _sqlite_engine()
    source = CostAttributionSource(vendor="aws", account_id="946646677266")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_bq_export_summary_daily (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      usage_date TEXT NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL,
                      service_name TEXT,
                      sku_name TEXT,
                      usage_type TEXT,
                      cost_driver_key TEXT,
                      region TEXT,
                      org TEXT,
                      repo TEXT,
                      target_branch TEXT,
                      vendor_tags_json TEXT,
                      author TEXT,
                      list_cost REAL,
                      effective_cost REAL,
                      credit_amount REAL,
                      net_cost REAL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_attribution_daily (
                      usage_date TEXT NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL,
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
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
                      attribution_key TEXT,
                      attribution_source TEXT,
                      attribution_status TEXT,
                      allocate_method TEXT,
                      employee_id INTEGER,
                      group_id INTEGER,
                      manager_id INTEGER,
                      usage_seconds REAL,
                      list_cost REAL,
                      effective_cost REAL,
                      credit_amount REAL,
                      net_cost REAL,
                      source_rows INTEGER,
                      dimension_hash TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE roster_employees (
                      id INTEGER PRIMARY KEY,
                      email TEXT,
                      github_id TEXT,
                      en_name TEXT,
                      group_id INTEGER,
                      manager_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE roster_groups (
                      id INTEGER PRIMARY KEY,
                      is_active INTEGER,
                      manager_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE resource_allocation (
                      id INTEGER PRIMARY KEY,
                      vendor TEXT NOT NULL,
                      account_id TEXT,
                      vendor_tags_json TEXT NOT NULL,
                      icost_owner_email TEXT,
                      icost_service TEXT,
                      icost_project TEXT,
                      icost_service_exec_id TEXT,
                      valid_from TEXT,
                      valid_to TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_employees (
                      id, email, github_id, en_name, group_id, manager_id
                    ) VALUES
                      (1, 'alice@pingcap.com', 'alice', 'Alice', 10, 100),
                      (2, 'bob@pingcap.com', 'bob', 'Bob', 20, 200),
                      (3, 'carol@pingcap.com', 'carol', 'Carol', 30, 300),
                      (4, 'dave@pingcap.com', 'dave', 'Dave', 40, 400)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_groups (id, is_active, manager_id)
                    VALUES (10, 1, 100), (20, 1, 200), (30, 1, 300), (40, 1, 400)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO resource_allocation (
                      id, vendor, account_id, vendor_tags_json, icost_owner_email,
                      icost_service, icost_project, icost_service_exec_id, valid_from, valid_to
                    ) VALUES
                      (
                        1, 'aws', '946646677266',
                        '{"cluster":"cluster-1","shared_pool":"pool-1"}', 'bob@pingcap.com',
                        'TestInfra', 'project-x', 'exec-1', NULL, NULL
                      ),
                      (
                        2, 'aws', '946646677266',
                        '{"cluster":"cluster-2","shared_pool":"pool-1"}', 'carol@pingcap.com',
                        'TestInfra', 'project-y', 'exec-2', NULL, NULL
                      )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      usage_date, vendor, account_id, service_name, sku_name, region, org, repo,
                      usage_type, cost_driver_key, target_branch, vendor_tags_json, author, list_cost,
                      effective_cost, credit_amount, net_cost
                    ) VALUES
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'BoxUsage', 'us-east-1', 'pingcap', 'tidb',
                        'USE1-BoxUsage:m6i.large', 'compute', 'master', NULL,
                        'alice', 10, 10, 0, 10
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-1","shared_pool":"pool-1"}',
                        NULL, 20, 20, 0, 20
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'AuthClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-1","shared_pool":"pool-1"}',
                        'alice', 7, 7, 0, 7
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'FakeAuthorClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-no-allocation","shared_pool":"pool-1"}',
                        'alice', 11, 11, 0, 11
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-2","shared_pool":"pool-1"}',
                        NULL, 30, 30, 0, 30
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'SharedUsage', 'us-east-1', NULL, NULL,
                        'USE1-DataTransfer-Out-Bytes', 'data_transfer', NULL,
                        '{"shared_pool":"pool-1"}',
                        NULL, 5, 5, 0, 5
                      )
                    """
                )
            )

        summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=source,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
            tcms_allocation_table="resource_allocation",
        )

        assert summary.rows_inserted == 7
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                      sku_name,
                      region,
                      usage_type,
                      cost_driver_key,
                      author,
                      owner,
                      service,
                      project,
                      attribution_source,
                      attribution_status,
                      allocate_method,
                      employee_id,
                      ROUND(net_cost, 2) AS net_cost
                    FROM cost_attribution_daily
                    ORDER BY COALESCE(allocate_method, ''), sku_name, project
                    """
                )
            ).mappings().all()
            total_net_cost = connection.execute(
                text("SELECT ROUND(SUM(net_cost), 2) FROM cost_attribution_daily")
            ).scalar_one()

        def find_row(*, sku_name, project=None, author=None, allocate_method=None):
            for row in rows:
                if (
                    row["sku_name"] == sku_name
                    and row["project"] == project
                    and row["author"] == author
                    and row["allocate_method"] == allocate_method
                ):
                    return row
            raise AssertionError(f"row not found: {sku_name=} {project=} {author=}")

        author_row = find_row(sku_name="BoxUsage", author="alice")
        cluster_x_row = find_row(
            sku_name="ClusterUsage", project="project-x", allocate_method="logical"
        )
        authored_cluster_row = find_row(
            sku_name="AuthClusterUsage",
            project="project-x",
            author="alice",
            allocate_method="logical",
        )
        cluster_y_row = find_row(
            sku_name="ClusterUsage", project="project-y", allocate_method="logical"
        )
        shared_x_row = find_row(
            sku_name="SharedUsage", project="project-x", allocate_method="shared_weighted"
        )
        shared_y_row = find_row(
            sku_name="SharedUsage", project="project-y", allocate_method="shared_weighted"
        )

        fake_author_row = find_row(sku_name="FakeAuthorClusterUsage", author="alice")

        assert total_net_cost == 83.0
        assert {row["region"] for row in rows} == {"us-east-1"}
        assert cluster_x_row["usage_type"] == "USE1-BoxUsage:m6i.large"
        assert cluster_x_row["cost_driver_key"] == "compute"
        assert shared_x_row["usage_type"] == "USE1-DataTransfer-Out-Bytes"
        assert shared_x_row["cost_driver_key"] == "data_transfer"
        assert author_row["owner"] is None
        assert author_row["attribution_source"] == "missing_author"
        assert author_row["attribution_status"] == "unattributed"
        assert author_row["employee_id"] is None
        assert fake_author_row["owner"] is None
        assert fake_author_row["attribution_source"] == "missing_label_allocation"
        assert fake_author_row["attribution_status"] == "unattributed"
        assert fake_author_row["employee_id"] is None
        assert cluster_x_row["owner"] == "bob@pingcap.com"
        assert cluster_x_row["service"] == "TestInfra"
        assert cluster_x_row["attribution_source"] == "owner_email"
        assert cluster_x_row["attribution_status"] == "matched"
        assert cluster_x_row["employee_id"] == 2
        assert authored_cluster_row["owner"] == "bob@pingcap.com"
        assert authored_cluster_row["attribution_source"] == "owner_email"
        assert authored_cluster_row["attribution_status"] == "matched"
        assert authored_cluster_row["employee_id"] == 2
        assert authored_cluster_row["net_cost"] == 7.0
        assert cluster_y_row["owner"] == "carol@pingcap.com"
        assert cluster_y_row["service"] == "TestInfra"
        assert cluster_y_row["attribution_source"] == "owner_email"
        assert cluster_y_row["attribution_status"] == "matched"
        assert cluster_y_row["employee_id"] == 3
        assert shared_x_row["service"] == "TestInfra"
        assert shared_x_row["attribution_source"] == "label_shared"
        assert shared_x_row["attribution_status"] == "shared"
        assert shared_x_row["net_cost"] == 2.37
        assert shared_y_row["service"] == "TestInfra"
        assert shared_y_row["attribution_source"] == "label_shared"
        assert shared_y_row["attribution_status"] == "shared"
        assert shared_y_row["net_cost"] == 2.63

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO resource_allocation (
                      id, vendor, account_id, vendor_tags_json, icost_owner_email,
                      icost_service, icost_project, icost_service_exec_id, valid_from, valid_to
                    ) VALUES (
                      3, 'aws', '946646677266', :vendor_tags_json,
                      'dave@pingcap.com', 'TestInfra', 'project-pool', 'exec-pool',
                      NULL, NULL
                    )
                    """
                ),
                {"vendor_tags_json": '{"cluster":null,"shared_pool":"pool-1"}'},
            )

        subset_summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=source,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
            tcms_allocation_table="resource_allocation",
        )

        assert subset_summary.rows_inserted == 6
        with engine.begin() as connection:
            subset_rows = connection.execute(
                text(
                    """
                    SELECT
                      sku_name,
                      owner,
                      project,
                      attribution_source,
                      attribution_status,
                      allocate_method,
                      employee_id,
                      ROUND(net_cost, 2) AS net_cost
                    FROM cost_attribution_daily
                    ORDER BY COALESCE(allocate_method, ''), sku_name, project
                    """
                )
            ).mappings().all()
            subset_total_net_cost = connection.execute(
                text("SELECT ROUND(SUM(net_cost), 2) FROM cost_attribution_daily")
            ).scalar_one()

        assert subset_total_net_cost == 83.0
        subset_authored_cluster = next(
            row
            for row in subset_rows
            if row["sku_name"] == "AuthClusterUsage" and row["project"] == "project-x"
        )
        subset_cluster_x = next(
            row
            for row in subset_rows
            if row["sku_name"] == "ClusterUsage" and row["project"] == "project-x"
        )
        subset_cluster_y = next(
            row
            for row in subset_rows
            if row["sku_name"] == "ClusterUsage" and row["project"] == "project-y"
        )
        subset_shared = next(row for row in subset_rows if row["sku_name"] == "SharedUsage")
        subset_fake_author = next(
            row for row in subset_rows if row["sku_name"] == "FakeAuthorClusterUsage"
        )

        assert subset_authored_cluster["net_cost"] == 7.0
        assert subset_cluster_x["net_cost"] == 20.0
        assert subset_cluster_y["net_cost"] == 30.0
        assert subset_fake_author["owner"] == "dave@pingcap.com"
        assert subset_fake_author["attribution_source"] == "owner_email"
        assert subset_fake_author["attribution_status"] == "matched"
        assert subset_shared["owner"] == "dave@pingcap.com"
        assert subset_shared["project"] == "project-pool"
        assert subset_shared["attribution_source"] == "owner_email"
        assert subset_shared["attribution_status"] == "matched"
        assert subset_shared["allocate_method"] == "vendor_tag"
        assert subset_shared["employee_id"] == 4
        assert subset_shared["net_cost"] == 5.0

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM resource_allocation"))
            connection.execute(
                text(
                    """
                    INSERT INTO resource_allocation (
                      id, vendor, account_id, vendor_tags_json, icost_owner_email,
                      icost_service, icost_project, icost_service_exec_id, valid_from, valid_to
                    ) VALUES (
                      4, 'aws', '946646677266',
                      '{"cluster":"cluster-1","shared_pool":"pool-1"}',
                      'bob@pingcap.com', 'TestInfra', 'expired-project', 'expired-exec',
                      NULL, '2026-06-30'
                    )
                    """
                )
            )

        expired_tcms_summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=source,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
            tcms_allocation_table="resource_allocation",
        )

        assert expired_tcms_summary.rows_inserted == 6
        with engine.begin() as connection:
            fallback_rows = connection.execute(
                text(
                    """
                    SELECT
                      sku_name,
                      attribution_source,
                      attribution_status,
                      allocate_method,
                      service,
                      project,
                      ROUND(SUM(net_cost), 2) AS net_cost
                    FROM cost_attribution_daily
                    GROUP BY
                      sku_name,
                      attribution_source,
                      attribution_status,
                      allocate_method,
                      service,
                      project
                    ORDER BY sku_name
                    """
                )
            ).mappings().all()
            fallback_total_net_cost = connection.execute(
                text("SELECT ROUND(SUM(net_cost), 2) FROM cost_attribution_daily")
            ).scalar_one()

        assert fallback_total_net_cost == 83.0
        fallback_auth = next(row for row in fallback_rows if row["sku_name"] == "BoxUsage")
        fallback_auth_cluster = next(
            row for row in fallback_rows if row["sku_name"] == "AuthClusterUsage"
        )
        fallback_cluster = next(row for row in fallback_rows if row["sku_name"] == "ClusterUsage")
        fallback_shared = next(row for row in fallback_rows if row["sku_name"] == "SharedUsage")

        assert fallback_auth["attribution_status"] == "matched"
        assert fallback_auth_cluster["attribution_status"] == "matched"
        assert fallback_cluster["attribution_source"] == "missing_label_allocation"
        assert fallback_cluster["attribution_status"] == "unattributed"
        assert fallback_cluster["service"] is None
        assert fallback_cluster["project"] is None
        assert fallback_cluster["net_cost"] == 50.0
        assert fallback_shared["attribution_source"] == "missing_label_allocation"
        assert fallback_shared["attribution_status"] == "unattributed"
        assert fallback_shared["allocate_method"] is None
        assert fallback_shared["net_cost"] == 5.0
    finally:
        engine.dispose()


def test_run_refresh_aws_summary_with_tcms_keeps_non_roster_owner_email() -> None:
    engine = _sqlite_engine()
    source = CostAttributionSource(vendor="aws", account_id="946646677266")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_bq_export_summary_daily (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      usage_date TEXT NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL,
                      service_name TEXT,
                      sku_name TEXT,
                      usage_type TEXT,
                      cost_driver_key TEXT,
                      region TEXT,
                      org TEXT,
                      repo TEXT,
                      target_branch TEXT,
                      vendor_tags_json TEXT,
                      author TEXT,
                      list_cost REAL,
                      effective_cost REAL,
                      credit_amount REAL,
                      net_cost REAL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE cost_attribution_daily (
                      usage_date TEXT NOT NULL,
                      vendor TEXT NOT NULL,
                      account_id TEXT NOT NULL,
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
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
                      attribution_key TEXT,
                      attribution_source TEXT,
                      attribution_status TEXT,
                      allocate_method TEXT,
                      employee_id INTEGER,
                      group_id INTEGER,
                      manager_id INTEGER,
                      usage_seconds REAL,
                      list_cost REAL,
                      effective_cost REAL,
                      credit_amount REAL,
                      net_cost REAL,
                      source_rows INTEGER,
                      dimension_hash TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE roster_employees (
                      id INTEGER PRIMARY KEY,
                      email TEXT,
                      github_id TEXT,
                      en_name TEXT,
                      group_id INTEGER,
                      manager_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE roster_groups (
                      id INTEGER PRIMARY KEY,
                      is_active INTEGER,
                      manager_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE resource_allocation (
                      id INTEGER PRIMARY KEY,
                      vendor TEXT NOT NULL,
                      account_id TEXT,
                      vendor_tags_json TEXT NOT NULL,
                      icost_owner_email TEXT,
                      icost_service TEXT,
                      icost_project TEXT,
                      icost_service_exec_id TEXT,
                      valid_from TEXT,
                      valid_to TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_employees (
                      id, email, github_id, en_name, group_id, manager_id
                    ) VALUES
                      (2, 'bob@pingcap.com', 'bob', 'Bob', 20, 200)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_groups (id, is_active, manager_id)
                    VALUES (20, 1, 200)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO resource_allocation (
                      id, vendor, account_id, vendor_tags_json, icost_owner_email,
                      icost_service, icost_project, icost_service_exec_id, valid_from, valid_to
                    ) VALUES
                      (
                        1, 'aws', '946646677266',
                        '{"cluster":"cluster-external"}', 'external@vendor.com',
                        'TestInfra', 'project-external', 'exec-external', NULL, NULL
                      ),
                      (
                        2, 'aws', '946646677266',
                        '{"cluster":"cluster-internal"}', 'bob@pingcap.com',
                        'TestInfra', 'project-internal', 'exec-internal', NULL, NULL
                      )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      usage_date, vendor, account_id, service_name, sku_name, region, org, repo,
                      target_branch, vendor_tags_json, author, list_cost,
                      effective_cost, credit_amount, net_cost
                    ) VALUES
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ExternalOwnerUsage', 'us-east-1', NULL, NULL, NULL,
                        '{"cluster":"cluster-external"}',
                        NULL, 10, 10, 0, 10
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'InternalOwnerUsage', 'us-east-1', NULL, NULL, NULL,
                        '{"cluster":"cluster-internal"}',
                        NULL, 20, 20, 0, 20
                      )
                    """
                )
            )

        summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=source,
            start_date=date(2026, 7, 14),
            end_date=date(2026, 7, 14),
            tcms_allocation_table="resource_allocation",
        )

        assert summary.rows_inserted == 2
        with engine.begin() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                      sku_name,
                      owner,
                      attribution_key,
                      attribution_source,
                      attribution_status,
                      employee_id,
                      group_id,
                      manager_id
                    FROM cost_attribution_daily
                    ORDER BY sku_name
                    """
                )
            ).mappings().all()

        external_row = next(
            row for row in rows if row["sku_name"] == "ExternalOwnerUsage"
        )
        internal_row = next(
            row for row in rows if row["sku_name"] == "InternalOwnerUsage"
        )

        assert external_row["owner"] == "external@vendor.com"
        assert external_row["attribution_key"] == "owner_email:external@vendor.com"
        assert external_row["attribution_source"] == "owner_email"
        assert external_row["attribution_status"] == "unmatched"
        assert external_row["employee_id"] is None
        assert external_row["group_id"] is None
        assert external_row["manager_id"] is None
        assert internal_row["owner"] == "bob@pingcap.com"
        assert internal_row["attribution_key"] == "employee:2"
        assert internal_row["attribution_source"] == "owner_email"
        assert internal_row["attribution_status"] == "matched"
        assert internal_row["employee_id"] == 2
        assert internal_row["group_id"] == 20
        assert internal_row["manager_id"] == 200
    finally:
        engine.dispose()


def test_insert_sql_contains_roster_matching_and_daily_dimensions() -> None:
    sql = str(_INSERT_ATTRIBUTION_DAILY)

    assert "LEFT JOIN roster_employees github_employee" in sql
    assert "LEFT JOIN roster_employees override_employee" in sql
    assert "override_employee.is_active" not in sql
    assert "github_employee.is_active" not in sql
    assert "email_employee.is_active" not in sql
    assert "normalized_employee.is_active" not in sql
    assert "flaky-claw" in sql
    assert "yinsu@pingcap.com" in sql
    assert "ti-chi-bot" in sql
    assert "wei.zheng@pingcap.com" in sql
    assert "author_override" in sql
    assert "LEFT JOIN roster_employees normalized_employee" in sql
    assert "LOWER(github_employee.github_id) = LOWER(raw.author)" in sql
    assert "SUBSTRING_INDEX(email_employee.email, '@', 1)" in sql
    assert "LEFT JOIN roster_groups matched_group" in sql
    assert "author_github" in sql
    assert "author_email" in sql
    assert "author_normalized" in sql
    assert "missing_author" in sql
    assert "resource_name" in sql
    assert "target_branch" in sql
    assert "SHA2(" in sql
    assert "{normalized_" not in sql


def test_summary_insert_sql_uses_summary_source_and_nullable_resource_columns() -> None:
    sql = str(_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY)

    assert "FROM cost_bq_export_summary_daily summary" in sql
    assert "summary.service_name" in sql
    assert "summary.sku_name" in sql
    assert "summary.usage_type" in sql
    assert "summary.cost_driver_key" in sql
    assert "NULL AS resource_name" in sql
    assert "NULL AS usage_seconds" in sql
    assert "target_branch" in sql
    assert "LEFT JOIN roster_employees github_employee" in sql
    assert "LEFT JOIN roster_employees override_employee" in sql
    assert "override_employee.is_active" not in sql
    assert "github_employee.is_active" not in sql
    assert "email_employee.is_active" not in sql
    assert "normalized_employee.is_active" not in sql
    assert "LOWER(github_employee.github_id) = LOWER(summary.author)" in sql
    assert "author_override" in sql
    assert "author_normalized" in sql
    assert "SHA2(" in sql
    assert "{normalized_" not in sql


def test_tcms_table_identifier_is_quoted_and_validated() -> None:
    assert _quote_table_identifier("tcms_cost.resource_allocation") == (
        "`tcms_cost`.`resource_allocation`"
    )

    with pytest.raises(ValueError, match="Invalid tcms allocation table identifier"):
        _quote_table_identifier("tcms-cost.resource_allocation")


def test_aws_summary_insert_statements_include_tcms_allocation() -> None:
    statements = _summary_insert_statements(
        source=CostAttributionSource(vendor="aws", account_id="946646677266"),
        tcms_allocation_table="tcms_cost.resource_allocation",
    )

    assert len(statements) == 2
    logical_sql = str(statements[0])
    shared_sql = str(statements[1])

    assert "`tcms_cost`.`resource_allocation` allocation_raw" in logical_sql
    assert "summary.vendor_tags_json" in logical_sql
    assert "JSON_EXTRACT(summary.vendor_tags_json, '$.shared_pool')" in logical_sql
    assert "JSON_EXTRACT(summary.vendor_tags_json, '$.cluster')" in logical_sql
    assert "match_tags_json" in logical_sql
    assert "JSON_REMOVE" in logical_sql
    assert "missing_label_allocation" in logical_sql
    assert "allocation_raw.icost_owner_email AS owner_email" in logical_sql
    assert "allocation_raw.icost_service_exec_id AS service_exec_id" in logical_sql
    assert "allocate_method" in logical_sql
    assert "vendor_tag" in logical_sql

    assert "WITH allocation_match AS" in shared_sql
    assert "ROW_NUMBER() OVER" in shared_sql
    assert "JSON_REMOVE" in shared_sql
    assert "label_shared" in shared_sql
    assert "shared_weighted" in shared_sql
    assert "allocation_count IS NULL" in shared_sql
    assert "logical.service" in shared_sql
    assert "summary.sku_name" in shared_sql


def test_non_aws_summary_insert_uses_existing_statement() -> None:
    statements = _summary_insert_statements(
        source=CostAttributionSource(vendor="gcp", account_id="pingcap-testing-account"),
        tcms_allocation_table="tcms_cost.resource_allocation",
    )

    assert statements == (_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,)
