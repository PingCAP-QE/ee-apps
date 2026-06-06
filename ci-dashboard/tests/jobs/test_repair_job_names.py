from __future__ import annotations

from sqlalchemy import text

from ci_dashboard.jobs.repair_job_names import run_repair_job_names


def test_run_repair_job_names_backfills_short_names(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_builds (
                  id, source_prow_job_id, namespace, job_name, job_type, state,
                  org, repo, repo_full_name, url, start_time, completion_time, cloud_phase
                ) VALUES
                  (1, 'prow-build-1', 'prow', 'pull-check-deps', 'presubmit', 'success',
                   'pingcap', 'tidb', 'pingcap/tidb',
                   'https://prow.tidb.net/view/gs/prow-tidb-logs/pr-logs/pull/pingcap_tidb/1/pull-check-deps/1/',
                   '2026-04-20 10:00:00', '2026-04-20 10:10:00', 'GCP'),
                  (2, 'prow-build-2', 'prow', 'pingcap/tidb/pull-unit', 'presubmit', 'success',
                   'pingcap', 'tidb', 'pingcap/tidb',
                   'https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/pull-unit/2/',
                   '2026-04-20 10:00:00', '2026-04-20 10:10:00', 'GCP')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO ci_l1_pod_lifecycle (
                  source_project, namespace_name, pod_name, pod_uid, build_system,
                  repo_full_name, job_name, ci_job
                ) VALUES
                  ('gcp-project', 'ci', 'pod-a', 'uid-a', 'JENKINS', 'pingcap/tidb', 'pull-check-deps', NULL),
                  ('gcp-project', 'ci', 'pod-b', 'uid-b', 'JENKINS', 'pingcap/tidb', NULL, 'pingcap/tidb/pull-unit'),
                  ('gcp-project', 'ci', 'pod-c', 'uid-c', 'JENKINS', 'pingcap/tidb', 'pingcap/tidb/pull-int', 'pingcap/tidb/pull-int')
                """
            )
        )

    summary = run_repair_job_names(sqlite_engine)

    assert summary.build_short_before == 1
    assert summary.build_short_after == 0
    assert summary.pod_short_before == 2
    assert summary.pod_short_after == 0
    assert summary.build_rows_updated == 1
    assert summary.pod_rows_updated == 2

    with sqlite_engine.begin() as connection:
        build_names = connection.execute(
            text("SELECT id, job_name FROM ci_l1_builds ORDER BY id")
        ).mappings().all()
        pod_names = connection.execute(
            text("SELECT pod_name, job_name FROM ci_l1_pod_lifecycle ORDER BY pod_name")
        ).mappings().all()

    assert build_names == [
        {"id": 1, "job_name": "pingcap/tidb/pull-check-deps"},
        {"id": 2, "job_name": "pingcap/tidb/pull-unit"},
    ]
    assert pod_names == [
        {"pod_name": "pod-a", "job_name": "pingcap/tidb/pull-check-deps"},
        {"pod_name": "pod-b", "job_name": "pingcap/tidb/pull-unit"},
        {"pod_name": "pod-c", "job_name": "pingcap/tidb/pull-int"},
    ]
