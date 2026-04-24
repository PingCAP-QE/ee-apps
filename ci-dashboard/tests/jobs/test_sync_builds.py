import json
from datetime import datetime

import pytest
from sqlalchemy import text

from ci_dashboard.common.config import DatabaseSettings, JobSettings, Settings
from ci_dashboard.jobs.state_store import get_job_state
from ci_dashboard.jobs.sync_builds import (
    _coerce_optional_bool,
    _duration_seconds,
    _parse_json_object,
    _required,
    extract_spec_fields,
    extract_status_fields,
    map_build_row,
    normalize_build_batch,
    run_sync_builds,
    run_sync_builds_for_time_window,
)


def test_extract_status_fields_reads_camel_case_payload() -> None:
    row = {
        "status": {
            "pendingTime": "2026-04-13T10:02:00Z",
            "build_id": "299",
            "pod_name": "pod-1",
        }
    }

    fields = extract_status_fields(row)

    assert fields["pending_time"] == datetime(2026, 4, 13, 10, 2, 0)
    assert fields["build_id"] == "299"
    assert fields["pod_name"] == "pod-1"


def test_extract_spec_fields_reads_head_sha() -> None:
    row = {
        "spec": {
            "refs": {
                "pulls": [
                    {
                        "sha": "0123456789abcdef0123456789abcdef01234567",
                    }
                ]
            }
        }
    }

    fields = extract_spec_fields(row)

    assert fields["head_sha"] == "0123456789abcdef0123456789abcdef01234567"


def test_map_build_row_derives_v1_fields() -> None:
    row = {
        "id": 101,
        "prowJobId": "11111111-1111-1111-1111-111111111111",
        "namespace": "prow",
        "jobName": "ghpr_unit_test",
        "type": "presubmit",
        "state": "failure",
        "optional": 0,
        "report": 1,
        "org": "pingcap",
        "repo": "tidb",
        "base_ref": "master",
        "pull": 123,
        "context": "unit-test",
        "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect",
        "author": "alice",
        "retest": 0,
        "event_guid": "guid-1",
        "startTime": "2026-04-13T10:00:00Z",
        "completionTime": "2026-04-13T10:15:00Z",
        "spec": {
            "refs": {
                "pulls": [
                    {
                        "sha": "0123456789abcdef0123456789abcdef01234567",
                    }
                ]
            }
        },
        "status": {
            "pendingTime": "2026-04-13T10:02:00Z",
            "build_id": "299",
            "pod_name": "pod-1",
        },
    }

    build = map_build_row(row)

    assert build.repo_full_name == "pingcap/tidb"
    assert build.is_pr_build is True
    assert build.pr_number == 123
    assert build.normalized_build_url == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/"
    assert build.cloud_phase == "GCP"
    assert build.build_system == "JENKINS"
    assert build.head_sha == "0123456789abcdef0123456789abcdef01234567"
    assert build.queue_wait_seconds == 120
    assert build.run_seconds == 780
    assert build.total_seconds == 900
    assert build.build_id == "299"
    assert build.pod_name == "pod-1"
    assert build.failure_category is None


def test_map_build_row_allows_missing_optional_status_fields() -> None:
    row = {
        "id": 201,
        "prowJobId": "job-201",
        "namespace": "prow",
        "jobName": "unit",
        "type": "presubmit",
        "state": "success",
        "org": "pingcap",
        "repo": "tidb",
        "url": "https://do.pingcap.net/job/abc",
        "startTime": "2026-04-13T10:00:00Z",
        "status": {},
        "spec": {},
    }

    build = map_build_row(row)

    assert build.build_id is None
    assert build.pod_name is None
    assert build.cloud_phase == "IDC"
    assert build.build_system == "JENKINS"


def test_map_build_row_recognizes_prow_native_gcp_jobs() -> None:
    row = {
        "id": 202,
        "prowJobId": "job-202",
        "namespace": "prow",
        "jobName": "pull-check-deps",
        "type": "presubmit",
        "state": "success",
        "org": "pingcap",
        "repo": "tidb",
        "url": "https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/67929/pull-check-deps/2046433013478199296",
        "startTime": "2026-04-13T10:00:00Z",
        "status": {},
        "spec": {},
    }

    build = map_build_row(row)

    assert build.cloud_phase == "GCP"
    assert build.build_system == "PROW_NATIVE"


def test_normalize_build_batch_skips_malformed_rows() -> None:
    good_row = {
        "id": 301,
        "prowJobId": "job-301",
        "namespace": "prow",
        "jobName": "unit",
        "type": "presubmit",
        "state": "success",
        "org": "pingcap",
        "repo": "tidb",
        "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/301/display/redirect",
        "startTime": "2026-04-13T10:00:00Z",
        "spec": {},
        "status": {},
    }
    bad_row = {
        "id": 302,
        "prowJobId": "job-302",
        "namespace": "prow",
        "jobName": "unit",
        "type": "presubmit",
        "state": "triggered",
        "org": "pingcap",
        "repo": "tidb",
        "url": "",
        "spec": {},
        "status": {},
    }

    build_rows, skipped_rows = normalize_build_batch([good_row, bad_row])

    assert [row.source_prow_job_id for row in build_rows] == ["job-301"]
    assert skipped_rows == 1


def test_sync_builds_end_to_end_with_sqlite(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prow_jobs (
                  id, prowJobId, namespace, jobName, type, state, optional, report,
                  org, repo, base_ref, pull, context, url, author, retest, event_guid,
                  startTime, completionTime, spec, status
                ) VALUES (
                  :id, :prowJobId, :namespace, :jobName, :type, :state, :optional, :report,
                  :org, :repo, :base_ref, :pull, :context, :url, :author, :retest, :event_guid,
                  :startTime, :completionTime, :spec, :status
                )
                """
            ),
            [
                {
                    "id": 1,
                    "prowJobId": "job-1",
                    "namespace": "prow",
                    "jobName": "ghpr_unit_test",
                    "type": "presubmit",
                    "state": "failure",
                    "optional": 0,
                    "report": 1,
                    "org": "pingcap",
                    "repo": "tidb",
                    "base_ref": "master",
                    "pull": 100,
                    "context": "unit",
                    "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/100/display/redirect",
                    "author": "alice",
                    "retest": 0,
                    "event_guid": "guid-1",
                    "startTime": "2026-04-13T10:00:00Z",
                    "completionTime": "2026-04-13T10:10:00Z",
                    "spec": json.dumps(
                        {
                            "refs": {
                                "pulls": [
                                    {"sha": "0123456789abcdef0123456789abcdef01234567"}
                                ]
                            }
                        }
                    ),
                    "status": json.dumps({"pendingTime": "2026-04-13T10:01:00Z", "build_id": "100"}),
                },
                {
                    "id": 2,
                    "prowJobId": "job-2",
                    "namespace": "prow",
                    "jobName": "nightly",
                    "type": "periodic",
                    "state": "success",
                    "optional": 1,
                    "report": 0,
                    "org": "pingcap",
                    "repo": "tidb",
                    "base_ref": None,
                    "pull": None,
                    "context": None,
                    "url": "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/nightly/2/",
                    "author": "bob",
                    "retest": None,
                    "event_guid": None,
                    "startTime": "2026-04-13T11:00:00Z",
                    "completionTime": "2026-04-13T11:20:00Z",
                    "spec": json.dumps({}),
                    "status": json.dumps({"pod_name": "pod-2"}),
                },
            ],
        )

    settings = Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=1),
        log_level="INFO",
    )
    summary = run_sync_builds(sqlite_engine, settings)

    assert summary.batches_processed == 2
    assert summary.source_rows_scanned == 2
    assert summary.rows_written == 2
    assert summary.rows_skipped == 0
    assert summary.last_source_prow_row_id == 2

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT source_prow_job_id, repo_full_name, is_pr_build, cloud_phase, normalized_build_url
                        , build_system
                    FROM ci_l1_builds
                    ORDER BY source_prow_row_id
                    """
                )
            ).mappings()
        )
        state = get_job_state(connection, "ci-sync-builds")

    assert [row["source_prow_job_id"] for row in rows] == ["job-1", "job-2"]
    assert rows[0]["repo_full_name"] == "pingcap/tidb"
    assert rows[0]["is_pr_build"] == 1
    assert rows[0]["cloud_phase"] == "GCP"
    assert rows[0]["build_system"] == "JENKINS"
    assert rows[1]["cloud_phase"] == "IDC"
    assert rows[1]["build_system"] == "JENKINS"
    assert rows[1]["normalized_build_url"] == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/nightly/2/"
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark == {"last_source_prow_row_id": 2}


def test_sync_builds_skips_malformed_rows_and_keeps_progress(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prow_jobs (
                  id, prowJobId, namespace, jobName, type, state, org, repo, url, startTime, spec, status
                ) VALUES (
                  :id, :prowJobId, :namespace, :jobName, :type, :state, :org, :repo, :url, :startTime, :spec, :status
                )
                """
            ),
            [
                {
                    "id": 10,
                    "prowJobId": "job-good",
                    "namespace": "prow",
                    "jobName": "good",
                    "type": "presubmit",
                    "state": "success",
                    "org": "pingcap",
                    "repo": "tidb",
                    "url": "https://do.pingcap.net/job/good",
                    "startTime": "2026-04-13T10:00:00Z",
                    "spec": json.dumps({}),
                    "status": json.dumps({}),
                },
                {
                    "id": 11,
                    "prowJobId": "job-bad",
                    "namespace": "prow",
                    "jobName": "bad",
                    "type": "presubmit",
                    "state": "triggered",
                    "org": "pingcap",
                    "repo": "tidb",
                    "url": "",
                    "startTime": None,
                    "spec": json.dumps({}),
                    "status": json.dumps({}),
                },
            ],
        )

    settings = Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=10),
        log_level="INFO",
    )

    summary = run_sync_builds(sqlite_engine, settings)

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text("SELECT source_prow_job_id FROM ci_l1_builds ORDER BY source_prow_row_id")
            ).scalars()
        )
        state = get_job_state(connection, "ci-sync-builds")

    assert summary.source_rows_scanned == 2
    assert summary.rows_written == 1
    assert summary.rows_skipped == 1
    assert summary.last_source_prow_row_id == 11
    assert rows == ["job-good"]
    assert state is not None
    assert state.last_status == "succeeded"
    assert state.watermark == {"last_source_prow_row_id": 11}


def test_sync_builds_marks_failure_state_on_fetch_error(sqlite_engine, monkeypatch) -> None:
    settings = Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=10),
        log_level="INFO",
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("ci_dashboard.jobs.sync_builds.fetch_source_rows", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        run_sync_builds(sqlite_engine, settings)

    with sqlite_engine.begin() as connection:
        state = get_job_state(connection, "ci-sync-builds")

    assert state is not None
    assert state.last_status == "failed"
    assert "boom" in (state.last_error or "")


def test_sync_builds_time_window_is_repeatable(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prow_jobs (
                  id, prowJobId, namespace, jobName, type, state, optional, report,
                  org, repo, base_ref, pull, context, url, author, retest, event_guid,
                  startTime, completionTime, spec, status
                ) VALUES (
                  :id, :prowJobId, :namespace, :jobName, :type, :state, :optional, :report,
                  :org, :repo, :base_ref, :pull, :context, :url, :author, :retest, :event_guid,
                  :startTime, :completionTime, :spec, :status
                )
                """
            ),
            [
                {
                    "id": 10,
                    "prowJobId": "job-before",
                    "namespace": "prow",
                    "jobName": "unit",
                    "type": "presubmit",
                    "state": "success",
                    "optional": 0,
                    "report": 1,
                    "org": "pingcap",
                    "repo": "tidb",
                    "base_ref": "master",
                    "pull": 101,
                    "context": "unit",
                    "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/10/display/redirect",
                    "author": "alice",
                    "retest": 0,
                    "event_guid": "guid-10",
                    "startTime": "2026-04-12T23:59:59Z",
                    "completionTime": "2026-04-13T00:09:59Z",
                    "spec": json.dumps({}),
                    "status": json.dumps({}),
                },
                {
                    "id": 11,
                    "prowJobId": "job-in-1",
                    "namespace": "prow",
                    "jobName": "unit",
                    "type": "presubmit",
                    "state": "failure",
                    "optional": 0,
                    "report": 1,
                    "org": "pingcap",
                    "repo": "tidb",
                    "base_ref": "master",
                    "pull": 102,
                    "context": "unit",
                    "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/11/display/redirect",
                    "author": "alice",
                    "retest": 0,
                    "event_guid": "guid-11",
                    "startTime": "2026-04-13T00:00:00Z",
                    "completionTime": "2026-04-13T00:10:00Z",
                    "spec": json.dumps({}),
                    "status": json.dumps({}),
                },
                {
                    "id": 12,
                    "prowJobId": "job-in-2",
                    "namespace": "prow",
                    "jobName": "unit",
                    "type": "presubmit",
                    "state": "success",
                    "optional": 0,
                    "report": 1,
                    "org": "pingcap",
                    "repo": "tidb",
                    "base_ref": "master",
                    "pull": 103,
                    "context": "unit",
                    "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/12/display/redirect",
                    "author": "bob",
                    "retest": 0,
                    "event_guid": "guid-12",
                    "startTime": "2026-04-13T12:00:00Z",
                    "completionTime": "2026-04-13T12:10:00Z",
                    "spec": json.dumps({}),
                    "status": json.dumps({}),
                },
            ],
        )

    settings = Settings(
        database=DatabaseSettings(
            url="sqlite+pysqlite:///:memory:",
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=None,
        ),
        jobs=JobSettings(batch_size=1),
        log_level="INFO",
    )

    first_summary = run_sync_builds_for_time_window(
        sqlite_engine,
        settings,
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )
    second_summary = run_sync_builds_for_time_window(
        sqlite_engine,
        settings,
        start_time_from=datetime(2026, 4, 13, 0, 0, 0),
        start_time_to=datetime(2026, 4, 14, 0, 0, 0),
    )

    assert first_summary.rows_written == 2
    assert second_summary.rows_written == 2

    with sqlite_engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT source_prow_job_id
                    FROM ci_l1_builds
                    ORDER BY source_prow_row_id
                    """
                )
            ).scalars()
        )
        state = get_job_state(connection, "ci-sync-builds")

    assert rows == ["job-in-1", "job-in-2"]
    assert state is None


def test_sync_builds_helper_branches() -> None:
    assert _parse_json_object(None) == {}
    assert _parse_json_object("{}") == {}
    assert _duration_seconds(datetime(2026, 4, 13, 10, 0, 1), datetime(2026, 4, 13, 10, 0, 0)) is None
    assert _coerce_optional_bool("yes") is True
    assert _coerce_optional_bool("0") is False

    with pytest.raises(ValueError, match="Missing required field"):
        _required({}, "id")

    with pytest.raises(ValueError, match="Unsupported JSON object"):
        _parse_json_object("[]")
