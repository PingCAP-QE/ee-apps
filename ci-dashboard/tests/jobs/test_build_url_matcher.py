from ci_dashboard.jobs.build_url_matcher import (
    build_job_url,
    classify_build_system,
    classify_cloud_phase,
    normalize_build_url,
    normalized_job_path_from_key,
)


def test_normalize_build_url_strips_known_prefix_and_redirect() -> None:
    raw = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect"
    assert normalize_build_url(raw) == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/"


def test_normalize_build_url_preserves_public_host() -> None:
    raw = "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect"
    assert normalize_build_url(raw) == "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/"


def test_normalize_build_url_handles_none_and_blank() -> None:
    assert normalize_build_url(None) is None
    assert normalize_build_url("   ") is None


def test_normalize_build_url_supports_relative_paths_and_internal_jenkins_host() -> None:
    assert (
        normalize_build_url("/job/pingcap/job/tidb/job/ghpr_unit_test/299/display/redirect")
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/"
    )
    assert (
        normalize_build_url(
            "http://jenkins.jenkins.svc.cluster.local/job/pingcap/job/tidb/job/ghpr_unit_test/299/"
        )
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/299/"
    )
    assert normalize_build_url("https://example.test/not-a-build") is None


def test_classify_cloud_phase_uses_host_prefix() -> None:
    assert classify_cloud_phase("https://prow.tidb.net/jenkins/job/example") == "GCP"
    assert classify_cloud_phase("https://prow.tidb.net/view/gs/prow-tidb-logs/job/example") == "GCP"
    assert classify_cloud_phase("https://do.pingcap.net/job/example") == "IDC"


def test_classify_build_system_distinguishes_jenkins_and_prow_native() -> None:
    assert classify_build_system("https://prow.tidb.net/jenkins/job/example") == "JENKINS"
    assert classify_build_system("https://prow.tidb.net/view/gs/prow-tidb-logs/job/example") == "PROW_NATIVE"
    assert classify_build_system("https://do.pingcap.net/job/example") == "JENKINS"
    assert classify_build_system("https://example.test/job/example") == "UNKNOWN"


def test_build_job_url_uses_cloud_phase_host() -> None:
    assert (
        build_job_url("/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test", "GCP")
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test"
    )
    assert (
        build_job_url("/jenkins/job/pingcap/job/tidb/job/nightly", "IDC")
        == "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/nightly"
    )
    assert build_job_url("https://prow.tidb.net/jenkins/job/example", "GCP") == "https://prow.tidb.net/jenkins/job/example/"
    assert build_job_url("   ", "GCP") is None
    assert build_job_url(None, "GCP") is None


def test_classify_build_system_handles_missing_url() -> None:
    assert classify_build_system(None) == "UNKNOWN"
    assert classify_build_system("") == "UNKNOWN"


def test_normalized_job_path_from_key_strips_only_numeric_build_suffix() -> None:
    assert (
        normalized_job_path_from_key("https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1061/")
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/"
    )
    assert normalized_job_path_from_key("https://prow.tidb.net/jenkins/job/job-4/") == "https://prow.tidb.net/jenkins/job/job-4/"
    assert normalized_job_path_from_key(None) is None
    assert normalized_job_path_from_key("https://example.test/not-a-build") is None
