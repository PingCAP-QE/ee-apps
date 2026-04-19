from ci_dashboard.jobs.build_url_matcher import (
    build_job_url,
    classify_cloud_phase,
    normalize_build_url,
    normalized_job_path_from_key,
)


def test_normalize_build_url_strips_known_prefix_and_redirect() -> None:
    raw = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect"
    assert normalize_build_url(raw) == "/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299"


def test_normalize_build_url_handles_none_and_blank() -> None:
    assert normalize_build_url(None) is None
    assert normalize_build_url("   ") is None


def test_classify_cloud_phase_uses_gcp_prefix() -> None:
    assert classify_cloud_phase("https://prow.tidb.net/jenkins/job/example") == "GCP"
    assert classify_cloud_phase("https://do.pingcap.net/job/example") == "IDC"


def test_build_job_url_uses_cloud_phase_host() -> None:
    assert (
        build_job_url("/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test", "GCP")
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test"
    )
    assert (
        build_job_url("/jenkins/job/pingcap/job/tidb/job/nightly", "IDC")
        == "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/nightly"
    )


def test_normalized_job_path_from_key_strips_only_numeric_build_suffix() -> None:
    assert (
        normalized_job_path_from_key("/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1061")
        == "/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test"
    )
    assert normalized_job_path_from_key("/jenkins/job/job-4") == "/jenkins/job/job-4"
