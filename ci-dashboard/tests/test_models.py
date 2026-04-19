from __future__ import annotations

from datetime import datetime

from ci_dashboard.common.models import NormalizedBuildRow


def test_normalized_build_row_as_db_params_converts_booleans() -> None:
    row = NormalizedBuildRow(
        source_prow_row_id=1,
        source_prow_job_id="job-1",
        namespace="prow",
        job_name="job",
        job_type="presubmit",
        state="success",
        optional=True,
        report=False,
        org="pingcap",
        repo="tidb",
        repo_full_name="pingcap/tidb",
        base_ref="master",
        pr_number=123,
        is_pr_build=True,
        context=None,
        url="https://example.test",
        normalized_build_key="/job/1",
        author="alice",
        retest=True,
        event_guid=None,
        build_id=None,
        pod_name=None,
        pending_time=None,
        start_time=datetime(2026, 4, 13, 10, 0, 0),
        completion_time=None,
        queue_wait_seconds=None,
        run_seconds=None,
        total_seconds=None,
        head_sha=None,
        target_branch=None,
        cloud_phase="IDC",
        is_flaky=False,
        is_retry_loop=True,
        has_flaky_case_match=False,
        failure_category=None,
        failure_subcategory=None,
    )

    params = row.as_db_params()

    assert params["optional"] == 1
    assert params["report"] == 0
    assert params["is_pr_build"] == 1
    assert params["is_retry_loop"] == 1
    assert params["retest"] == 1
