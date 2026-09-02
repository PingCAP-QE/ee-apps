import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from cost_insight.jobs import state_store
from cost_insight.jobs.job_keys import source_job_name
from cost_insight.jobs.refresh_attribution_daily import (
    _INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,
    SUMMARY_JOB_NAME,
    CostAttributionSource,
    _positive_rowcount,
    _quote_table_identifier,
    _summary_insert_statements,
    _watermark,
    normalized_identity_sql,
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
        connection.execute(
            text(
                """
                CREATE TABLE cost_kubernetes_pvc_pod_mapping (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  vendor TEXT NOT NULL,
                  account_id TEXT NOT NULL,
                  persistent_volume_name TEXT NOT NULL,
                  pod_uid TEXT NOT NULL,
                  author TEXT,
                  org TEXT,
                  repo TEXT,
                  UNIQUE(vendor, account_id, persistent_volume_name, pod_uid)
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


def test_run_refresh_aws_attribution_requires_tcms_before_writing() -> None:
    engine = _sqlite_engine()
    source = CostAttributionSource(vendor="aws", account_id="946646677266")
    try:
        with pytest.raises(ValueError, match="tcms_allocation_table is required"):
            run_refresh_cost_attribution_from_summary(
                engine,
                source=source,
                start_date=date(2026, 8, 10),
                end_date=date(2026, 8, 10),
            )

        with engine.begin() as connection:
            assert (
                state_store.get_job_state(
                    connection,
                    source_job_name(
                        SUMMARY_JOB_NAME,
                        vendor=source.vendor,
                        account_id=source.account_id,
                    ),
                )
                is None
            )
    finally:
        engine.dispose()


def test_run_refresh_aws_attribution_requires_readable_tcms_before_writing() -> None:
    engine = _sqlite_engine()
    source = CostAttributionSource(vendor="aws", account_id="946646677266")
    try:
        with pytest.raises(OperationalError, match="no such table"):
            run_refresh_cost_attribution_from_summary(
                engine,
                source=source,
                start_date=date(2026, 8, 10),
                end_date=date(2026, 8, 10),
                tcms_allocation_table="missing_resource_allocation",
            )

        with engine.begin() as connection:
            assert (
                state_store.get_job_state(
                    connection,
                    source_job_name(
                        SUMMARY_JOB_NAME,
                        vendor=source.vendor,
                        account_id=source.account_id,
                    ),
                )
                is None
            )
    finally:
        engine.dispose()


def test_run_refresh_attribution_from_summary_marks_success(monkeypatch) -> None:
    engine = _sqlite_engine()
    executed = []

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_allocation_publication (
                  publication_name TEXT PRIMARY KEY,
                  active_allocation_version TEXT
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO cost_allocation_publication VALUES ('dashboard', 'stale-version')")
        )
        connection.execute(
            text(
                """
                CREATE TABLE cost_resource_serving_publication (
                  basis_key TEXT,
                  vendor TEXT,
                  account_id TEXT,
                  usage_date TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_resource_serving_publication VALUES
                  ('native', 'gcp', 'pingcap-testing-account', '2026-05-09'),
                  ('native', 'gcp', 'pingcap-testing-account', '2026-05-11'),
                  ('eq_allocated', 'gcp', 'pingcap-testing-account', '2026-05-09'),
                  ('native', 'aws', '946646677266', '2026-05-09')
                """
            )
        )

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
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM cost_allocation_publication")
            ).scalar_one() == 0
            remaining_publications = connection.execute(
                text(
                    """
                    SELECT basis_key, vendor, account_id, usage_date
                    FROM cost_resource_serving_publication
                    ORDER BY basis_key, vendor, usage_date
                    """
                )
            ).all()
        assert remaining_publications == [
            ("eq_allocated", "gcp", "pingcap-testing-account", "2026-05-09"),
            ("native", "aws", "946646677266", "2026-05-09"),
            ("native", "gcp", "pingcap-testing-account", "2026-05-11"),
        ]
    finally:
        engine.dispose()


def test_refresh_failure_rolls_back_publication_invalidation(monkeypatch) -> None:
    engine = _sqlite_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cost_allocation_publication (
                  publication_name TEXT PRIMARY KEY,
                  active_allocation_version TEXT
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO cost_allocation_publication VALUES ('dashboard', 'active-version')")
        )

    original_execute = Connection.execute

    def fake_execute(self, statement, params=None, *args, **kwargs):
        sql = str(statement)
        if "DELETE FROM cost_attribution_daily" in sql or (
            "FROM cost_bq_export_summary_daily summary" in sql
        ):
            class Result:
                rowcount = 1

            return Result()
        return original_execute(self, statement, params, *args, **kwargs)

    def fail_after_invalidation(*args, **kwargs):
        raise RuntimeError("failed after publication invalidation")

    monkeypatch.setattr("sqlalchemy.engine.base.Connection.execute", fake_execute)
    monkeypatch.setattr(state_store, "mark_job_succeeded", fail_after_invalidation)

    try:
        with pytest.raises(RuntimeError, match="failed after publication invalidation"):
            run_refresh_cost_attribution_from_summary(
                engine,
                source=SOURCE,
                start_date=date(2026, 5, 9),
                end_date=date(2026, 5, 10),
            )

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT active_allocation_version FROM cost_allocation_publication")
            ).scalar_one() == "active-version"
            state = state_store.get_job_state(
                connection,
                source_job_name(
                    SUMMARY_JOB_NAME,
                    vendor=SOURCE.vendor,
                    account_id=SOURCE.account_id,
                ),
            )
        assert state is not None
        assert state.last_status == "failed"
    finally:
        engine.dispose()


def test_summary_attribution_resolves_unambiguous_pvc_pod_owner() -> None:
    engine = _sqlite_engine()
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
                      resource_name TEXT,
                      vendor_tags_json TEXT,
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
                      author TEXT,
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
                      source_row_hash TEXT,
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
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
                      author TEXT,
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
                      attribution_key TEXT,
                      attribution_source TEXT,
                      attribution_status TEXT,
                      employee_id INTEGER,
                      group_id INTEGER,
                      manager_id INTEGER,
                      usage_seconds REAL,
                      list_cost REAL,
                      effective_cost REAL,
                      credit_amount REAL,
                      net_cost REAL,
                      source_rows INTEGER,
                      dimension_hash TEXT,
                      source_summary_row_hash TEXT
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
                    INSERT INTO roster_employees (id, email, github_id, en_name, group_id, manager_id)
                    VALUES
                      (1, 'alice@pingcap.com', 'alice', 'Alice', 10, 100),
                      (2, 'bob@pingcap.com', 'bob', 'Bob', 20, 200)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_groups (id, is_active, manager_id)
                    VALUES (10, 1, 100), (20, 1, 200)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_kubernetes_pvc_pod_mapping (
                      vendor, account_id, persistent_volume_name, pod_uid, author, org, repo
                    ) VALUES
                      (
                        'gcp', 'pingcap-testing-account', 'pvc-unique', 'uid-unique',
                        'alice', 'pingcap', 'tidb'
                      ),
                      (
                        'gcp', 'pingcap-testing-account', 'pvc-shared', 'uid-shared-a',
                        'alice', 'pingcap', 'tidb'
                      ),
                      (
                        'gcp', 'pingcap-testing-account', 'pvc-shared', 'uid-shared-b',
                        'bob', 'pingcap', 'tidb'
                      ),
                      (
                        'gcp', 'pingcap-testing-account', 'pvc-direct-author', 'uid-direct',
                        'alice', 'pingcap', 'tidb'
                      )
                    """
                )
            )
            connection.execute(
                text(
                    """
                        INSERT INTO cost_bq_export_summary_daily (
                          usage_date, vendor, account_id, service_name, sku_name,
                          resource_name, author, list_cost, effective_cost, credit_amount, net_cost,
                          source_row_hash
                    ) VALUES
                      (
                        '2026-08-16', 'gcp', 'pingcap-testing-account',
                            'Compute Engine', 'Persistent Disk', 'pvc-unique', NULL, 10, 10, 0, 10,
                            'summary-pvc-unique'
                      ),
                      (
                        '2026-08-16', 'gcp', 'pingcap-testing-account',
                            'Compute Engine', 'Persistent Disk', 'pvc-shared', NULL, 20, 20, 0, 20,
                            'summary-pvc-shared'
                      ),
                      (
                        '2026-08-16', 'gcp', 'pingcap-testing-account',
                            'Compute Engine', 'Persistent Disk', 'pvc-direct-author', 'bob', 30, 30, 0, 30,
                            'summary-pvc-direct-author'
                      )
                    """
                )
            )

        summary = run_refresh_cost_attribution_from_summary(
            engine,
            source=SOURCE,
            start_date=date(2026, 8, 16),
            end_date=date(2026, 8, 16),
        )

        assert summary.rows_inserted == 3
        with engine.begin() as connection:
            rows = {
                row["resource_name"]: dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT resource_name, author, org, repo, owner, attribution_source,
                               attribution_status, employee_id, net_cost, source_summary_row_hash
                        FROM cost_attribution_daily
                        """
                    )
                ).mappings()
            }

        assert rows["pvc-unique"] == {
            "resource_name": "pvc-unique",
            "author": "alice",
            "org": "pingcap",
            "repo": "tidb",
            "owner": "alice@pingcap.com",
            "attribution_source": "pvc_pod_github",
            "attribution_status": "matched",
            "employee_id": 1,
            "net_cost": 10.0,
            "source_summary_row_hash": "summary-pvc-unique",
        }
        assert rows["pvc-shared"] == {
            "resource_name": "pvc-shared",
            "author": None,
            "org": None,
            "repo": None,
            "owner": None,
            "attribution_source": "missing_author",
            "attribution_status": "unattributed",
            "employee_id": None,
            "net_cost": 20.0,
            "source_summary_row_hash": "summary-pvc-shared",
        }
        assert rows["pvc-direct-author"] == {
            "resource_name": "pvc-direct-author",
            "author": "bob",
            "org": None,
            "repo": None,
            "owner": "bob@pingcap.com",
            "attribution_source": "author_github",
            "attribution_status": "matched",
            "employee_id": 2,
            "net_cost": 30.0,
            "source_summary_row_hash": "summary-pvc-direct-author",
        }
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
                      resource_name TEXT,
                      vendor_tags_json TEXT,
                      source_schema_version TEXT,
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
                      author TEXT,
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
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
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
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
                      ),
                      (
                        4, 'aws', '946646677266',
                        '{"cluster":"cluster-source-label"}', 'bob@pingcap.com',
                        'TestInfra', 'project-x', 'exec-1', NULL, NULL
                      ),
                      (
                        5, 'aws', '946646677266',
                        '{"tenant":"tenant-0858"}', 'dave@pingcap.com',
                        'TestInfra', 'project-tenant', 'exec-tenant', NULL, NULL
                      ),
                      (
                        6, 'aws', '946646677266',
                        '{"tenant":"tenant-0858","shared_pool":"pool-tenant"}',
                        'carol@pingcap.com', 'TestInfra', 'project-tenant-pool',
                        'exec-tenant-pool', NULL, NULL
                      ),
                      (
                        7, 'aws', NULL, '{"cluster":"cluster-tenant"}',
                        'bob@pingcap.com', 'ClusterService', NULL,
                        'exec-global-cluster', NULL, NULL
                      )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO cost_bq_export_summary_daily (
                      usage_date, vendor, account_id, service_name, sku_name, region, org, repo,
                      usage_type, cost_driver_key, target_branch, vendor_tags_json, author,
                      source_schema_version, owner, service, project, service_exec_id, list_cost,
                      effective_cost, credit_amount, net_cost
                    ) VALUES
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'BoxUsage', 'us-east-1', 'pingcap', 'tidb',
                        'USE1-BoxUsage:m6i.large', 'compute', 'master', NULL,
                        'alice', NULL, NULL, NULL, NULL, NULL, 10, 10, 0, 10
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-1","shared_pool":"pool-1"}',
                        NULL, NULL, NULL, NULL, NULL, NULL, 20, 20, 0, 20
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'AuthClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-1","shared_pool":"pool-1"}',
                        'alice', NULL, NULL, NULL, NULL, NULL, 7, 7, 0, 7
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'FakeAuthorClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-no-allocation","shared_pool":"pool-1"}',
                        'alice', NULL, NULL, NULL, NULL, NULL, 11, 11, 0, 11
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ClusterUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-2","shared_pool":"pool-1"}',
                        NULL, NULL, NULL, NULL, NULL, NULL, 30, 30, 0, 30
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'SharedUsage', 'us-east-1', NULL, NULL,
                        'USE1-DataTransfer-Out-Bytes', 'data_transfer', NULL,
                        '{"shared_pool":"pool-1"}',
                        NULL, NULL, NULL, NULL, NULL, NULL, 5, 5, 0, 5
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'SplitLabelUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-source-label"}',
                        NULL, 'aws_split_cost_v1', 'dave@pingcap.com', 'direct-service',
                        'direct-project', 'direct-exec', 13, 13, 0, 13
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'LegacyLabelUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-source-label"}',
                        NULL, NULL, 'dave@pingcap.com', 'direct-service',
                        'direct-project', 'direct-exec', 17, 17, 0, 17
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'TenantUsage', 'us-east-1', 'tenant-0858', NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL, NULL,
                        'alice', NULL, NULL, NULL, NULL, NULL, 19, 19, 0, 19
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'TenantPoolUsage', 'us-east-1', 'tenant-0858', NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"shared_pool":"pool-tenant"}', 'alice', NULL, NULL, NULL, NULL, NULL,
                        23, 23, 0, 23
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'MissingTenantPoolUsage', 'us-east-1', NULL, NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"shared_pool":"pool-tenant"}', 'alice', NULL, NULL, NULL, NULL, NULL,
                        29, 29, 0, 29
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'TenantClusterUsage', 'us-east-1', 'tenant-0858', NULL,
                        'USE1-BoxUsage:m6i.large', 'compute', NULL,
                        '{"cluster":"cluster-tenant"}', 'alice', NULL, NULL, NULL, NULL, NULL,
                        31, 31, 0, 31
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

        assert summary.rows_inserted == 12
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
                      service_exec_id,
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
        shared_row = find_row(sku_name="SharedUsage")
        split_label_row = find_row(
            sku_name="SplitLabelUsage", project="direct-project", allocate_method="direct_label"
        )
        legacy_label_row = find_row(
            sku_name="LegacyLabelUsage", project="project-x", allocate_method="logical"
        )

        fake_author_row = find_row(sku_name="FakeAuthorClusterUsage", author="alice")
        tenant_row = find_row(
            sku_name="TenantUsage", project="project-tenant", author="alice", allocate_method="vendor_tag"
        )
        tenant_pool_row = find_row(
            sku_name="TenantPoolUsage",
            project="project-tenant-pool",
            author="alice",
            allocate_method="vendor_tag",
        )
        missing_tenant_pool_row = find_row(sku_name="MissingTenantPoolUsage", author="alice")
        tenant_cluster_row = find_row(
            sku_name="TenantClusterUsage",
            project="project-tenant",
            author="alice",
            allocate_method="logical",
        )

        assert total_net_cost == 215.0
        assert {row["region"] for row in rows} == {"us-east-1"}
        assert cluster_x_row["usage_type"] == "USE1-BoxUsage:m6i.large"
        assert cluster_x_row["cost_driver_key"] == "compute"
        assert shared_row["usage_type"] == "USE1-DataTransfer-Out-Bytes"
        assert shared_row["cost_driver_key"] == "data_transfer"
        assert author_row["owner"] is None
        assert author_row["attribution_source"] == "missing_author"
        assert author_row["attribution_status"] == "unattributed"
        assert author_row["employee_id"] is None
        assert fake_author_row["owner"] is None
        assert fake_author_row["attribution_source"] == "missing_label_allocation"
        assert fake_author_row["attribution_status"] == "unattributed"
        assert fake_author_row["employee_id"] is None
        assert tenant_row["owner"] == "dave@pingcap.com"
        assert tenant_row["attribution_source"] == "owner_email"
        assert tenant_row["attribution_status"] == "matched"
        assert tenant_row["employee_id"] == 4
        assert tenant_pool_row["owner"] == "carol@pingcap.com"
        assert tenant_pool_row["attribution_source"] == "owner_email"
        assert tenant_pool_row["attribution_status"] == "matched"
        assert tenant_pool_row["employee_id"] == 3
        assert missing_tenant_pool_row["owner"] is None
        assert missing_tenant_pool_row["attribution_source"] == "missing_author"
        assert missing_tenant_pool_row["attribution_status"] == "unattributed"
        assert missing_tenant_pool_row["employee_id"] is None
        assert tenant_cluster_row["owner"] == "bob@pingcap.com"
        assert tenant_cluster_row["service"] == "ClusterService"
        assert tenant_cluster_row["service_exec_id"] == "exec-global-cluster"
        assert tenant_cluster_row["attribution_source"] == "owner_email"
        assert tenant_cluster_row["attribution_status"] == "matched"
        assert tenant_cluster_row["employee_id"] == 2
        assert cluster_x_row["owner"] == "bob@pingcap.com"
        assert split_label_row["owner"] == "dave@pingcap.com"
        assert split_label_row["service"] == "direct-service"
        assert split_label_row["attribution_source"] == "source_label"
        assert split_label_row["employee_id"] == 4
        assert legacy_label_row["owner"] == "bob@pingcap.com"
        assert legacy_label_row["service"] == "TestInfra"
        assert legacy_label_row["attribution_source"] == "owner_email"
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
        assert shared_row["service"] is None
        assert shared_row["attribution_source"] == "missing_author"
        assert shared_row["attribution_status"] == "unattributed"
        assert shared_row["net_cost"] == 5.0

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

        assert subset_summary.rows_inserted == 12
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

        assert subset_total_net_cost == 215.0
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

        assert expired_tcms_summary.rows_inserted == 12
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

        assert fallback_total_net_cost == 215.0
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
        assert fallback_shared["attribution_source"] == "missing_author"
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
                      resource_name TEXT,
                      vendor_tags_json TEXT,
                      source_schema_version TEXT,
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
                      author TEXT,
                      owner TEXT,
                      service TEXT,
                      project TEXT,
                      service_exec_id TEXT,
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
                      source_allocation_scope TEXT NOT NULL DEFAULT 'direct',
                      namespace TEXT,
                      workload_name TEXT,
                      workload_type TEXT,
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
                      (2, 'bob@pingcap.com', 'bob', 'Bob', 20, 200),
                      (3, 'tiworkload@pingcap.com', 'tiworkload', 'TiWorkload', 30, 300)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO roster_groups (id, is_active, manager_id)
                    VALUES (20, 1, 200), (30, 1, 300)
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
                      target_branch, vendor_tags_json, author, source_schema_version, owner, list_cost,
                      effective_cost, credit_amount, net_cost
                    ) VALUES
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'ExternalOwnerUsage', 'us-east-1', NULL, NULL, NULL,
                        '{"cluster":"cluster-external"}',
                        NULL, NULL, NULL, 10, 10, 0, 10
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'InternalOwnerUsage', 'us-east-1', NULL, NULL, NULL,
                        '{"cluster":"cluster-internal"}',
                        NULL, NULL, NULL, 20, 20, 0, 20
                      ),
                      (
                        '2026-07-14', 'aws', '946646677266', 'AmazonEC2',
                        'EncodedOwnerUsage', 'us-east-1', NULL, NULL, NULL,
                        NULL,
                        NULL, 'aws_split_cost_v1', 'tiworkload_at_pingcap.com',
                        30, 30, 0, 30
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

        assert summary.rows_inserted == 3
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
        encoded_row = next(
            row for row in rows if row["sku_name"] == "EncodedOwnerUsage"
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
        assert encoded_row["owner"] == "tiworkload@pingcap.com"
        assert encoded_row["attribution_key"] == "employee:3"
        assert encoded_row["attribution_source"] == "source_label"
        assert encoded_row["attribution_status"] == "matched"
        assert encoded_row["employee_id"] == 3
    finally:
        engine.dispose()


def test_summary_insert_sql_uses_summary_source_and_nullable_resource_columns() -> None:
    sql = str(_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY)

    assert "FROM cost_bq_export_summary_daily summary" in sql
    assert "summary.service_name" in sql
    assert "summary.sku_name" in sql
    assert "summary.usage_type" in sql
    assert "summary.cost_driver_key" in sql
    assert "summary.resource_name" in sql
    assert "NULL AS usage_seconds" in sql
    assert "summary.vendor_tags_json" in sql
    assert "COALESCE(summary.source_allocation_scope, 'direct')" in sql
    assert "summary.namespace" in sql
    assert "summary.workload_name" in sql
    assert "summary.workload_type" in sql
    assert "target_branch" in sql
    assert "LEFT JOIN roster_employees github_employee" in sql
    assert "LEFT JOIN roster_employees override_employee" in sql
    assert "override_employee.is_active" not in sql
    assert "github_employee.is_active" not in sql
    assert "email_employee.is_active" not in sql
    assert "normalized_employee.is_active" not in sql
    assert "LOWER(github_employee.github_id) = LOWER(COALESCE(summary.author, pvc_mapping.author))" in sql
    assert "FROM cost_kubernetes_pvc_pod_mapping" in sql
    assert "HAVING COUNT(DISTINCT pod_uid) = 1" in sql
    assert "pvc_pod_github" in sql
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


def test_aws_summary_insert_statement_keeps_tcms_matching_without_pool_weighting() -> None:
    statements = _summary_insert_statements(
        source=CostAttributionSource(vendor="aws", account_id="946646677266"),
        tcms_allocation_table="tcms_cost.resource_allocation",
    )

    assert len(statements) == 1
    logical_sql = str(statements[0])

    assert "`tcms_cost`.`resource_allocation` allocation_raw" in logical_sql
    assert "summary.vendor_tags_json" in logical_sql
    assert "match_tags_json" in logical_sql
    assert "JSON_REMOVE" in logical_sql
    assert "missing_label_allocation" in logical_sql
    assert "allocation_raw.icost_owner_email AS owner_email" in logical_sql
    assert "allocation_raw.icost_service_exec_id AS service_exec_id" in logical_sql
    assert "allocate_method" in logical_sql
    assert "REPLACE(summary.owner, '_at_', '@')" in logical_sql
    assert "summary.owner IS NOT NULL THEN 'direct_label'" in logical_sql
    assert "vendor_tag" in logical_sql
    assert "source_allocation_scope" in logical_sql
    assert "workload_name" in logical_sql
    assert "workload_type" in logical_sql
    assert "summary.owner IS NOT NULL THEN 'source_label'" in logical_sql
    assert "shared_weighted" not in logical_sql
    assert "label_shared" not in logical_sql


def test_non_aws_summary_insert_uses_existing_statement() -> None:
    statements = _summary_insert_statements(
        source=CostAttributionSource(vendor="gcp", account_id="pingcap-testing-account"),
        tcms_allocation_table="tcms_cost.resource_allocation",
    )

    assert statements == (_INSERT_ATTRIBUTION_DAILY_FROM_SUMMARY,)
