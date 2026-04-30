from __future__ import annotations

from sqlalchemy import text

from ci_dashboard.common.config import (
    ArchiveSettings,
    DatabaseSettings,
    JenkinsSettings,
    JobSettings,
    Settings,
)
from ci_dashboard.jobs.archive_error_logs import (
    build_archive_object_ref,
    parse_archive_timestamp,
    redact_console_log,
    run_archive_error_logs,
)


def _settings() -> Settings:
    return Settings(
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
        jenkins=JenkinsSettings(http_timeout_seconds=5),
        archive=ArchiveSettings(
            build_limit=20,
            log_tail_bytes=1024,
            gcs_bucket="ci-dashboard-test",
            gcs_prefix="",
        ),
        log_level="INFO",
    )


class _FakeFetcher:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, int]] = []

    def fetch_console_tail(self, build_url: str, *, max_bytes: int) -> str:
        self.calls.append((build_url, max_bytes))
        return self.text


class _FakeFetcherWithFailedNodes(_FakeFetcher):
    def __init__(self, text: str, failed_node_text: str) -> None:
        super().__init__(text)
        self.failed_node_text = failed_node_text
        self.failed_node_calls: list[str] = []

    def fetch_failed_node_logs(self, build_url: str) -> str:
        self.failed_node_calls.append(build_url)
        return self.failed_node_text


class _FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def upload_text(self, *, bucket: str, object_name: str, text: str, content_type: str = "text/plain; charset=utf-8") -> str:
        del content_type
        self.calls.append((bucket, object_name, text))
        return f"gcs://{bucket}/{object_name}"


def _insert_build(
    sqlite_engine,
    *,
    build_id: int,
    state: str = "failure",
    build_system: str = "JENKINS",
    log_gcs_uri: str | None = None,
) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  id, source_prow_row_id, source_prow_job_id, namespace, job_name, job_type, state,
                  optional, report, org, repo, repo_full_name, base_ref, pr_number, is_pr_build,
                  context, url, normalized_build_url, author, start_time, completion_time,
                  total_seconds, target_branch, cloud_phase, build_system, log_gcs_uri
                ) VALUES (
                  :id, NULL, NULL, NULL, 'ghpr_unit_test', NULL, :state,
                  0, 1, 'pingcap', 'tidb', 'pingcap/tidb', 'master', :pr_number, 1,
                  'unit',
                  :url,
                  :normalized_build_url,
                  'alice', '2026-04-24 10:00:00', '2026-04-24 10:20:00',
                  1200, 'master', 'GCP', :build_system, :log_gcs_uri
                )
                """
            ),
            {
                "id": build_id,
                "state": state,
                "pr_number": build_id,
                "url": f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/{build_id}/display/redirect",
                "normalized_build_url": (
                    f"https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/{build_id}/"
                ),
                "build_system": build_system,
                "log_gcs_uri": log_gcs_uri,
            },
        )


def test_redact_console_log_masks_common_secrets() -> None:
    raw = (
        "token=abc123\n"
        "Bearer ghp_123456\n"
        "contact alice@example.com\n"
        "hit 10.8.9.10\n"
        "workspace /home/alice/project\n"
        "url https://x.test/path?token=s3cr3t\n"
    )

    redacted = redact_console_log(raw)

    assert "abc123" not in redacted
    assert "ghp_123456" not in redacted
    assert "alice@example.com" not in redacted
    assert "10.8.9.10" not in redacted
    assert "/home/alice/" not in redacted
    assert "token=s3cr3t" not in redacted
    assert "[REDACTED]" in redacted
    assert "[EMAIL]" in redacted
    assert "[INTERNAL_IP]" in redacted
    assert "/home/[USER]/" in redacted


def test_parse_archive_timestamp_supports_space_before_timezone_offset() -> None:
    parsed = parse_archive_timestamp("2026-04-24 10:00:00 +00:00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-04-24T10:00:00+00:00"


def test_run_archive_error_logs_archives_failed_jenkins_build(sqlite_engine) -> None:
    _insert_build(sqlite_engine, build_id=101)
    _insert_build(sqlite_engine, build_id=102, state="success")
    fetcher = _FakeFetcher("token=abc123\nfailure line\n")
    uploader = _FakeUploader()

    summary = run_archive_error_logs(
        sqlite_engine,
        _settings(),
        fetcher=fetcher,
        uploader=uploader,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_archived == 1
    assert summary.builds_failed == 0
    assert fetcher.calls == [
        ("https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/101/display/redirect", 1024)
    ]
    assert uploader.calls[0][0] == "ci-dashboard-test"
    assert uploader.calls[0][1] == "2604/101.log"
    assert "abc123" not in uploader.calls[0][2]

    with sqlite_engine.begin() as connection:
        row = connection.execute(
            text("SELECT log_gcs_uri FROM ci_l1_builds WHERE id = 101")
        ).mappings().one()

    assert row["log_gcs_uri"] == "gcs://ci-dashboard-test/2604/101.log"


def test_run_archive_error_logs_appends_failed_pipeline_node_logs(sqlite_engine) -> None:
    _insert_build(sqlite_engine, build_id=151)
    fetcher = _FakeFetcherWithFailedNodes(
        "root tail with Connection reset by peer\n",
        (
            "===== Jenkins failed pipeline node log =====\n"
            "parameter: run_real_tikv_tests.sh bazel_importintotest\n"
            "INFO: Build completed, 1 test FAILED, 5247 total actions\n"
        ),
    )
    uploader = _FakeUploader()

    summary = run_archive_error_logs(
        sqlite_engine,
        _settings(),
        build_id=151,
        fetcher=fetcher,
        uploader=uploader,
    )

    assert summary.builds_archived == 1
    assert fetcher.failed_node_calls == [
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/151/display/redirect"
    ]
    assert "Connection reset by peer" in uploader.calls[0][2]
    assert "Build completed, 1 test FAILED" in uploader.calls[0][2]


def test_run_archive_error_logs_skips_existing_archive_without_force(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=201,
        log_gcs_uri="gcs://ci-dashboard-test/2604/201.log",
    )
    fetcher = _FakeFetcher("failure line\n")
    uploader = _FakeUploader()

    summary = run_archive_error_logs(
        sqlite_engine,
        _settings(),
        fetcher=fetcher,
        uploader=uploader,
    )

    assert summary.builds_scanned == 0
    assert summary.builds_archived == 0
    assert fetcher.calls == []
    assert uploader.calls == []


def test_run_archive_error_logs_force_overwrites_existing_path(sqlite_engine) -> None:
    existing_uri = "gcs://ci-dashboard-test/custom/path/build-301.log"
    _insert_build(
        sqlite_engine,
        build_id=301,
        log_gcs_uri=existing_uri,
    )
    fetcher = _FakeFetcher("password=very-secret\n")
    uploader = _FakeUploader()

    summary = run_archive_error_logs(
        sqlite_engine,
        _settings(),
        build_id=301,
        force=True,
        fetcher=fetcher,
        uploader=uploader,
    )

    assert summary.builds_scanned == 1
    assert summary.builds_archived == 1
    assert uploader.calls[0][0] == "ci-dashboard-test"
    assert uploader.calls[0][1] == "custom/path/build-301.log"


def test_build_archive_object_ref_reuses_existing_uri_on_force(sqlite_engine) -> None:
    _insert_build(
        sqlite_engine,
        build_id=401,
        log_gcs_uri="gcs://ci-dashboard-test/custom/path/build-401.log",
    )

    with sqlite_engine.begin() as connection:
        build = connection.execute(text("SELECT id, log_gcs_uri FROM ci_l1_builds WHERE id = 401")).mappings().one()

    assert build_archive_object_ref(build, _settings(), force=True) == (
        "ci-dashboard-test",
        "custom/path/build-401.log",
    )


def test_build_archive_object_ref_supports_optional_prefix(sqlite_engine) -> None:
    _insert_build(sqlite_engine, build_id=501)

    with sqlite_engine.begin() as connection:
        build = connection.execute(
            text("SELECT id, start_time, completion_time, log_gcs_uri FROM ci_l1_builds WHERE id = 501")
        ).mappings().one()

    settings = _settings()
    settings = Settings(
        database=settings.database,
        jobs=settings.jobs,
        jenkins=settings.jenkins,
        archive=ArchiveSettings(
            build_limit=settings.archive.build_limit,
            log_tail_bytes=settings.archive.log_tail_bytes,
            gcs_bucket=settings.archive.gcs_bucket,
            gcs_prefix="ci-dashboard/jenkins",
        ),
        log_level=settings.log_level,
    )

    assert build_archive_object_ref(build, settings, force=False) == (
        "ci-dashboard-test",
        "ci-dashboard/jenkins/2604/501.log",
    )
