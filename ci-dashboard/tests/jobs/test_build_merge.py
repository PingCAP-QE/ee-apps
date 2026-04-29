from __future__ import annotations

import pytest

from ci_dashboard.jobs.build_merge import fetch_existing_build_targets, resolve_merge_target_id


def _candidate(candidate_id: int, normalized_build_url: str, source_prow_job_id: str | None):
    return {
        "id": candidate_id,
        "normalized_build_url": normalized_build_url,
        "source_prow_job_id": source_prow_job_id,
    }


def test_resolve_merge_target_id_prefers_exact_prow_job_match() -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/1/"
    existing_by_prow_job_id = {
        "prow-job-1": _candidate(101, normalized_build_url, "prow-job-1"),
        "prow-job-2": _candidate(102, normalized_build_url, "prow-job-2"),
    }
    existing_by_build_url = {
        normalized_build_url: [
            existing_by_prow_job_id["prow-job-1"],
            existing_by_prow_job_id["prow-job-2"],
        ]
    }

    target_id = resolve_merge_target_id(
        normalized_build_url=normalized_build_url,
        source_prow_job_id="prow-job-2",
        existing_by_prow_job_id=existing_by_prow_job_id,
        existing_by_build_url=existing_by_build_url,
    )

    assert target_id == 102


def test_resolve_merge_target_id_allows_new_row_when_url_is_reused_by_another_prow_job() -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/2/"
    existing_by_prow_job_id = {
        "old-prow-job": _candidate(201, normalized_build_url, "old-prow-job"),
    }
    existing_by_build_url = {
        normalized_build_url: [existing_by_prow_job_id["old-prow-job"]],
    }

    target_id = resolve_merge_target_id(
        normalized_build_url=normalized_build_url,
        source_prow_job_id="new-prow-job",
        existing_by_prow_job_id=existing_by_prow_job_id,
        existing_by_build_url=existing_by_build_url,
    )

    assert target_id is None


def test_resolve_merge_target_id_still_merges_into_single_unresolved_row() -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/3/"
    unresolved = _candidate(301, normalized_build_url, None)
    existing_by_build_url = {normalized_build_url: [unresolved]}

    target_id = resolve_merge_target_id(
        normalized_build_url=normalized_build_url,
        source_prow_job_id="prow-job-3",
        existing_by_prow_job_id={},
        existing_by_build_url=existing_by_build_url,
    )

    assert target_id == 301


def test_resolve_merge_target_id_without_source_prow_job_id_still_rejects_conflicts() -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/4/"
    existing_by_build_url = {
        normalized_build_url: [
            _candidate(401, normalized_build_url, "prow-job-4a"),
            _candidate(402, normalized_build_url, "prow-job-4b"),
        ]
    }

    with pytest.raises(ValueError, match="normalized_build_url already belongs"):
        resolve_merge_target_id(
            normalized_build_url=normalized_build_url,
            source_prow_job_id=None,
            existing_by_prow_job_id={},
            existing_by_build_url=existing_by_build_url,
        )


def test_fetch_existing_build_targets_returns_empty_maps_without_inputs(sqlite_engine) -> None:
    with sqlite_engine.begin() as connection:
        existing_by_prow_job_id, existing_by_build_url = fetch_existing_build_targets(
            connection,
            normalized_build_urls=[],
            source_prow_job_ids=[],
        )

    assert existing_by_prow_job_id == {}
    assert existing_by_build_url == {}


def test_resolve_merge_target_id_returns_none_without_build_url_or_candidates() -> None:
    assert (
        resolve_merge_target_id(
            normalized_build_url=None,
            source_prow_job_id="prow-job-5",
            existing_by_prow_job_id={},
            existing_by_build_url={},
        )
        is None
    )


def test_resolve_merge_target_id_without_source_prefers_single_candidate_or_unresolved_row() -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/5/"
    resolved = _candidate(501, normalized_build_url, "prow-job-5")
    unresolved = _candidate(502, normalized_build_url, None)

    assert (
        resolve_merge_target_id(
            normalized_build_url=normalized_build_url,
            source_prow_job_id=None,
            existing_by_prow_job_id={},
            existing_by_build_url={normalized_build_url: [resolved]},
        )
        == 501
    )
    assert (
        resolve_merge_target_id(
            normalized_build_url=normalized_build_url,
            source_prow_job_id=None,
            existing_by_prow_job_id={},
            existing_by_build_url={normalized_build_url: [resolved, unresolved]},
        )
        == 502
    )


def test_resolve_merge_target_id_chooses_oldest_unresolved_row_when_multiple_exist(caplog: pytest.LogCaptureFixture) -> None:
    normalized_build_url = "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/unit/6/"
    first_unresolved = _candidate(601, normalized_build_url, None)
    second_unresolved = _candidate(602, normalized_build_url, None)

    with caplog.at_level("WARNING"):
        target_id = resolve_merge_target_id(
            normalized_build_url=normalized_build_url,
            source_prow_job_id="prow-job-6",
            existing_by_prow_job_id={},
            existing_by_build_url={normalized_build_url: [first_unresolved, second_unresolved]},
            log_context={"worker": "jenkins"},
        )

    assert target_id == 601
    assert "multiple canonical build rows share normalized_build_url" in caplog.text
