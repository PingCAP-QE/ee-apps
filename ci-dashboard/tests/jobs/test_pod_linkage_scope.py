from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from ci_dashboard.jobs import sync_pods as pods

GCP = "gcp-project"
TENCENT = "tencent:ap-beijing:cls-example"
NOW = datetime(2026, 4, 21, 12)
PATH = "/jenkins/job/pingcap/job/tidb/job/test/123/"


def _url(cloud="GCP", path=PATH):
    return pods.jenkins_host_for_cloud(cloud) + path


def _build(
    connection,
    *,
    cloud="GCP",
    key=None,
    prow_id=None,
    start=NOW,
    name="agent-123-old",
    job="test",
    system="JENKINS",
    author="alice",
):
    key = key or _url(cloud)
    prow_id = prow_id or cloud
    connection.execute(
        text("""
        INSERT INTO ci_l1_builds (
            source_prow_job_id, normalized_build_url, cloud_phase, build_system,
            start_time, pod_name, state, org, repo, repo_full_name, job_name, author
        ) VALUES (:prow_id, :key, :cloud, :system, :start, :name, 'success',
                  'pingcap', 'tidb', 'pingcap/tidb', :job, :author)
    """),
        dict(
            prow_id=prow_id,
            key=key,
            cloud=cloud,
            system=system,
            start=start,
            name=name,
            job=job,
            author=author,
        ),
    )


def _row(source=GCP, **kwargs):
    return dict(
        source_project=source,
        namespace_name="jenkins-tidb",
        pod_uid="uid",
        pod_name="agent-123-new",
        scheduled_at=NOW,
        **kwargs,
    )


def _metadata(*, annotations=None, labels=None, created=NOW, observed=NOW):
    return pods.PodMetadataSnapshot("uid", labels or {}, annotations or {}, observed, created)


def _identity(row):
    return tuple(
        row[field] for field in ("source_project", "namespace_name", "pod_uid", "pod_name")
    )


def _resolve(connection, row, metadata=None, prefixes=None):
    return pods._load_build_metadata_map(
        connection,
        [row],
        pod_metadata_by_identity={_identity(row): metadata} if metadata else {},
        jenkins_pod_name_url_prefixes=prefixes,
    ).get(_identity(row), {})


def _save(
    connection,
    *,
    source=GCP,
    uid="uid",
    name="agent-123-new",
    scheduled=NOW,
    created=None,
    annotations=None,
    **kwargs,
):
    row = dict(
        source_project=source,
        cluster_name="cluster",
        location="region",
        namespace_name="jenkins-tidb",
        pod_uid=uid,
        pod_name=name,
        build_system="JENKINS",
        **pods._empty_pod_metadata_fields(),
        source_prow_job_id=None,
        normalized_build_url=None,
        repo_full_name=None,
        job_name=None,
        scheduled_at=scheduled,
        first_pulling_at=None,
        first_pulled_at=None,
        first_created_at=None,
        first_started_at=None,
        last_failed_scheduling_at=None,
        failed_scheduling_count=0,
        last_event_at=None,
        schedule_to_started_seconds=None,
    )
    if annotations is not None:
        row.update(_metadata(annotations=annotations, created=created).as_lifecycle_fields())
    row.update(pod_created_at=created, **kwargs)
    pods._upsert_pod_lifecycle(connection, [row])
    return row


@pytest.mark.parametrize("cloud,source", [("GCP", GCP), ("TENCENT", TENCENT)])
@pytest.mark.parametrize("evidence", ["internal", "relative", "public", "label", "name"])
def test_every_jenkins_path_uses_source_controller(sqlite_engine, cloud, source, evidence):
    with sqlite_engine.begin() as connection:
        # Same job name, Pod prefix, build number and internal controller in both clouds.
        _build(connection, cloud="GCP", start=NOW - timedelta(minutes=2))
        _build(connection, cloud="TENCENT", start=NOW - timedelta(minutes=1))
        metadata = {
            "internal": _metadata(
                annotations={
                    "buildUrl": "http://jenkins.jenkins.svc.cluster.local:8080" + PATH,
                    "runUrl": PATH.removeprefix("/jenkins/"),
                }
            ),
            "relative": _metadata(annotations={"runUrl": PATH.removeprefix("/jenkins/")}),
            "public": _metadata(annotations={"buildUrl": _url(cloud)}),
            "label": _metadata(
                labels={"org": "pingcap", "repo": "tidb", "jenkins/label": "test_123-random"},
                annotations={"ci_job": "pingcap/tidb/test"},
            ),
            "name": None,
        }[evidence]
        result = _resolve(connection, _row(source), metadata)
    assert result["source_prow_job_id"] == cloud
    assert result["normalized_build_url"] == _url(cloud)


@pytest.mark.parametrize("source,cloud", [(GCP, "GCP"), (TENCENT, "TENCENT")])
@pytest.mark.parametrize(
    "bad_url", ["https://unknown.invalid" + PATH, "//unknown.invalid" + PATH, "https://[broken"]
)
def test_unknown_metadata_blocks_even_valid_label_and_name(sqlite_engine, source, cloud, bad_url):
    with sqlite_engine.begin() as connection:
        _build(connection, cloud=cloud)
        result = _resolve(
            connection,
            _row(source),
            _metadata(
                annotations={"buildUrl": bad_url, "ci_job": "pingcap/tidb/test"},
                labels={"org": "pingcap", "repo": "tidb", "jenkins/label": "test_123-random"},
            ),
        )
    assert result == {"attribution_reason": "invalid_or_cross_scope_metadata_url"}


@pytest.mark.parametrize("source,cloud", [(GCP, "GCP"), (TENCENT, "TENCENT")])
def test_metadata_contradictions_and_existing_links_block_enrichment(sqlite_engine, source, cloud):
    other = "TENCENT" if cloud == "GCP" else "GCP"
    with sqlite_engine.begin() as connection:
        _build(connection, cloud=cloud)
        for annotations in (
            {"buildUrl": _url(other)},
            {"buildUrl": _url(cloud), "runUrl": PATH.replace("123", "124")},
        ):
            assert "attribution_reason" in _resolve(
                connection, _row(source), _metadata(annotations=annotations)
            )
        for fields in (
            {"normalized_build_url": _url(other)},
            {"normalized_build_url": _url(cloud).replace("123", "124")},
            {"source_prow_job_id": "a-different-build"},
        ):
            result = _resolve(
                connection, _row(source, **fields), _metadata(annotations={"buildUrl": _url(cloud)})
            )
            assert "attribution_reason" in result
            assert "author" not in result


def test_legacy_tencent_gcp_looking_build_key_is_not_reclassified(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _build(connection, cloud="TENCENT", key=_url("GCP"))
        result = _resolve(connection, _row(TENCENT), _metadata(annotations={"runUrl": PATH}))
        assert not result.get("_matched_build")
        assert result["normalized_build_url"] == _url("TENCENT")
        assert (
            pods._load_jenkins_pod_name_url_prefix_map(
                connection, cloud_phase="TENCENT", reference_time=NOW
            )
            == {}
        )
        assert connection.execute(
            text("SELECT normalized_build_url, cloud_phase FROM ci_l1_builds")
        ).one() == (_url("GCP"), "TENCENT")


@pytest.mark.parametrize("cloud,source", [("GCP", GCP), ("TENCENT", TENCENT)])
@pytest.mark.parametrize(
    "offset,matched", [(-86401, False), (-86400, True), (86400, True), (86401, False)]
)
def test_24_hour_guard_is_absolute_and_inclusive(sqlite_engine, cloud, source, offset, matched):
    with sqlite_engine.begin() as connection:
        _build(connection, cloud=cloud, start=NOW + timedelta(seconds=offset))
        result = _resolve(
            connection, _row(source), _metadata(annotations={"buildUrl": _url(cloud)})
        )
    assert bool(result.get("_matched_build")) is matched


def test_missing_time_only_retains_one_consistent_annotation_url(sqlite_engine):
    row = _row()
    row["scheduled_at"] = None
    with sqlite_engine.begin() as connection:
        _build(connection)
        result = _resolve(connection, row, _metadata(annotations={"runUrl": PATH}, created=None))
        assert result == {"build_system": "JENKINS", "normalized_build_url": _url()}
        result = _resolve(connection, row)
        assert not result.get("normalized_build_url")
        result = _resolve(
            connection,
            row,
            _metadata(
                labels={"org": "pingcap", "repo": "tidb", "jenkins/label": "test_123-random"},
                annotations={"ci_job": "pingcap/tidb/test"},
                created=None,
            ),
        )
        assert not result.get("normalized_build_url")
        # True Pod creation, not receipt/metadata observation, permits matching.
        assert _resolve(connection, row, _metadata(annotations={"runUrl": PATH}))["_matched_build"]


def test_candidates_and_prefix_ties_are_ambiguous_not_nearest_or_lexical(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _build(connection)
        _build(
            connection, prow_id="duplicate-controller-identity", start=NOW - timedelta(seconds=1)
        )
        result = _resolve(connection, _row(), _metadata(annotations={"runUrl": PATH}))
        assert result["attribution_reason"] == "ambiguous_build_candidates"
        connection.execute(text("DELETE FROM ci_l1_builds"))
        _build(connection, job="one", key=_url(path=PATH.replace("test", "one")))
        _build(connection, job="two", key=_url(path=PATH.replace("test", "two")), prow_id="two")
        prefixes = pods._load_jenkins_pod_name_url_prefix_map(connection, reference_time=NOW)
        assert "agent" not in prefixes
        assert (
            _resolve(connection, _row(), prefixes=prefixes)["attribution_reason"]
            == "ambiguous_prefix"
        )


def test_stronger_metadata_cannot_be_combined_with_different_matched_fallback(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _build(connection, name="unrelated-123-old")
        _build(
            connection, key=_url(path=PATH.replace("test", "other")), job="other", prow_id="other"
        )
        result = _resolve(connection, _row(), _metadata(annotations={"buildUrl": _url()}))
        assert result["attribution_reason"] == "ambiguous_build_candidates"
        connection.execute(text("DELETE FROM ci_l1_builds WHERE source_prow_job_id = 'GCP'"))
        # A missing strong fact is not permission to link the different fallback.
        result = _resolve(connection, _row(), _metadata(annotations={"buildUrl": _url()}))
        assert result["attribution_reason"] == "conflicting_candidate_urls"


@pytest.mark.parametrize("correct_fact", [False, True])
@pytest.mark.parametrize("has_time", [False, True])
def test_visible_legacy_cross_cloud_url_collision_cannot_leak_through_runtime_url_join(
    sqlite_engine, correct_fact, has_time
):
    row = _row()
    if not has_time:
        row["scheduled_at"] = None
    with sqlite_engine.begin() as connection:
        _build(connection, cloud="TENCENT", key=_url("GCP"), start=NOW - timedelta(days=30))
        if correct_fact:
            _build(connection)
        result = _resolve(connection, row, _metadata(annotations={"runUrl": PATH}, created=None))
        assert result == {"attribution_reason": "cross_scope_url_collision"}
        # Existing historical links remain unchanged, but receive no enrichment.
        _save(connection, source_prow_job_id="existing", normalized_build_url=_url())
        result = _resolve(connection, row, _metadata(annotations={"runUrl": PATH}, created=None))
        assert not result.get("author") and "attribution_reason" in result
        assert connection.execute(
            text("SELECT source_prow_job_id, pod_author FROM ci_l1_pod_lifecycle")
        ).one() == ("existing", None)


def test_url_key_with_an_old_duplicate_identity_is_ambiguous_outside_time_guard_too(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _build(connection)
        _build(connection, prow_id="old-key-reuse", start=NOW - timedelta(days=100))
        assert (
            _resolve(connection, _row(), _metadata(annotations={"runUrl": PATH}))[
                "attribution_reason"
            ]
            == "ambiguous_build_key"
        )


def test_supplied_prefix_map_cannot_bypass_cloud_window_or_discovery(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _build(connection)
        _build(connection, cloud="TENCENT")
        foreign = pods._load_jenkins_pod_name_url_prefix_map(connection, reference_time=NOW)
        assert (
            _resolve(connection, _row(TENCENT), prefixes=foreign)["source_prow_job_id"] == "TENCENT"
        )
        forged_plain = {"agent": _url().removesuffix("123/").replace("test", "wrong")}
        assert _resolve(connection, _row(), prefixes=forged_plain)["source_prow_job_id"] == "GCP"
        stale = pods._load_jenkins_pod_name_url_prefix_map(
            connection, reference_time=NOW - timedelta(days=40)
        )
        assert _resolve(connection, _row(), prefixes=stale)["source_prow_job_id"] == "GCP"


@pytest.mark.parametrize("cloud,source", [("GCP", GCP), ("TENCENT", TENCENT)])
def test_prow_name_fallback_has_cloud_system_controller_time_and_uniqueness_guards(
    sqlite_engine, cloud, source
):
    row = _row(source)
    row["namespace_name"] = "prow-test-pods"
    with sqlite_engine.begin() as connection:
        for candidate_cloud in ("GCP", "TENCENT"):
            _build(
                connection,
                cloud=candidate_cloud,
                key=_url(candidate_cloud, "/view/gs/bucket/job/123/"),
                system="PROW_NATIVE",
                name=row["pod_name"],
            )
        assert _resolve(connection, row)["source_prow_job_id"] == cloud
        _build(
            connection,
            cloud=cloud,
            key=_url(cloud, "/view/gs/bucket/job/124/"),
            system="PROW_NATIVE",
            name=row["pod_name"],
            prow_id="second",
        )
        assert _resolve(connection, row)["attribution_reason"] == "ambiguous_build_candidates"
        # Exact Prow ID is stronger than name reuse and does not use time heuristics.
        metadata = _metadata(labels={"prow.k8s.io/id": cloud}, created=None)
        row["scheduled_at"] = None
        assert _resolve(connection, row, metadata)["source_prow_job_id"] == cloud
        connection.execute(text("DELETE FROM ci_l1_builds WHERE source_prow_job_id = 'second'"))
        connection.execute(
            text("UPDATE ci_l1_builds SET start_time = :start WHERE cloud_phase = :cloud"),
            {"start": NOW - timedelta(days=2), "cloud": cloud},
        )
        row["scheduled_at"] = NOW
        assert not _resolve(connection, row).get("_matched_build")


def test_cross_source_reconcile_uses_saved_scope_and_true_creation(sqlite_engine, monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_POD_WATCH_CLOUD_PHASE", "GCP")
    with sqlite_engine.begin() as connection:
        for cloud, source in (("GCP", GCP), ("TENCENT", TENCENT)):
            _build(connection, cloud=cloud)
            _save(
                connection, source=source, scheduled=None, created=NOW, annotations={"runUrl": PATH}
            )
    first = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=NOW - timedelta(days=1),
        start_time_to=NOW + timedelta(days=1),
        source_project=TENCENT,
    )
    assert first.reconciled_rows_updated == first.reconciled_rows_scanned == 1
    with sqlite_engine.begin() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT source_prow_job_id FROM ci_l1_pod_lifecycle WHERE source_project = :source"
                ),
                {"source": GCP},
            ).scalar_one()
            is None
        )
    all_sources = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=NOW - timedelta(days=1),
        start_time_to=NOW + timedelta(days=1),
    )
    assert all_sources.reconciled_rows_updated == 1
    with sqlite_engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT source_project, source_prow_job_id, scheduled_at, pod_created_at, pod_annotations_json FROM ci_l1_pod_lifecycle ORDER BY id"
                )
            )
            .mappings()
            .all()
        )
    assert [row["source_prow_job_id"] for row in rows] == ["GCP", "TENCENT"]
    assert all(
        row["scheduled_at"] is None and row["pod_created_at"] and row["pod_annotations_json"]
        for row in rows
    )


def test_reconcile_bounded_cursor_freezes_ids_and_retries_overlapping_sweep(
    sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as connection:
        _save(connection, uid="first", name="opaque-first")
        _save(connection, uid="no-time", name="opaque-no-time", scheduled=None)
        _save(connection, uid="third", name="opaque-third")
    options = dict(
        start_time_from=NOW - timedelta(days=1),
        start_time_to=NOW + timedelta(days=1),
        source_project=GCP,
        batch_size=1,
        max_rows=1,
    )
    summary = pods.run_reconcile_pod_linkage_for_time_window(sqlite_engine, **options)
    assert summary.reconciled_rows_scanned == summary.reconciled_rows_skipped == 1
    assert summary.reconcile_after_id == 1
    assert summary.reconcile_through_id == 3
    assert summary.reconcile_missing_time_rows == 1
    assert not summary.reconcile_complete
    with sqlite_engine.begin() as connection:
        _save(connection, uid="new-arrival", name="opaque-fourth")
    resumed = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        **options,
        after_id=summary.reconcile_after_id,
        through_id=summary.reconcile_through_id,
    )
    assert resumed.reconcile_after_id == 3
    assert resumed.reconcile_complete
    assert resumed.reconciled_rows_scanned == 1
    # A new sweep, not a permanent forward cursor, retries earlier skipped rows.
    sweep = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine, **{**options, "max_rows": 100}
    )
    assert sweep.reconciled_rows_scanned == 3
    assert sweep.reconcile_after_id == 4


def test_new_rows_during_engine_and_connection_sweeps_stay_above_frozen_ceiling(
    sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as connection:
        _save(connection, uid="first", name="opaque-first")
        _save(connection, uid="second", name="opaque-second")
    original = pods._reconcile_lifecycle_batch
    inserted = False

    def insert_during_sweep(connection, rows, **kwargs):
        nonlocal inserted
        if rows and not inserted:
            inserted = True
            _save(connection, uid="new-arrival", name="opaque-new")
        return original(connection, rows, **kwargs)

    monkeypatch.setattr(pods, "_reconcile_lifecycle_batch", insert_during_sweep)
    options = dict(
        start_time_from=NOW - timedelta(days=1), start_time_to=NOW + timedelta(days=1), batch_size=1
    )
    summary = pods.run_reconcile_pod_linkage_for_time_window(sqlite_engine, **options)
    assert summary.reconciled_rows_scanned == 2
    assert summary.reconcile_after_id == summary.reconcile_through_id == 2
    assert summary.reconcile_complete
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM ci_l1_pod_lifecycle WHERE pod_uid = 'new-arrival'"))
        seen = []

        def insert_and_record(connection, rows, **kwargs):
            if rows:
                seen.extend(row["pod_uid"] for row in rows)
            return insert_during_sweep(connection, rows, **kwargs)

        inserted = False
        monkeypatch.setattr(pods, "_reconcile_lifecycle_batch", insert_and_record)
        assert pods._reconcile_lifecycle_rows_in_time_window(connection, **options) == 0
        assert seen == ["first", "second"]


def test_utc_window_is_half_open_and_unscheduled_creation_is_selected(sqlite_engine):
    with sqlite_engine.begin() as connection:
        _save(connection, uid="before", scheduled=NOW - timedelta(seconds=1))
        _save(connection, uid="included", scheduled=None, created=NOW)
        _save(connection, uid="excluded", scheduled=NOW + timedelta(days=1))
    summary = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=NOW.replace(tzinfo=UTC),
        start_time_to=(NOW + timedelta(days=1)).replace(tzinfo=UTC),
    )
    assert summary.reconciled_rows_scanned == 1
    assert summary.reconcile_after_id == summary.reconcile_through_id == 2


def test_cli_commits_batches_but_connection_caller_rolls_back_entire_sweep(
    sqlite_engine, monkeypatch, caplog
):
    with sqlite_engine.begin() as connection:
        _build(connection)
        _save(connection, uid="one")
        _save(connection, uid="two")
    original = pods._reconcile_lifecycle_batch
    calls = 0

    def fail_second(connection, rows, **kwargs):
        nonlocal calls
        if not rows:
            return original(connection, rows, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError("crash second batch")
        return original(connection, rows, **kwargs)

    monkeypatch.setattr(pods, "_reconcile_lifecycle_batch", fail_second)
    options = dict(
        start_time_from=NOW - timedelta(days=1), start_time_to=NOW + timedelta(days=1), batch_size=1
    )
    with pytest.raises(RuntimeError, match="second batch"):
        pods.run_reconcile_pod_linkage_for_time_window(sqlite_engine, **options)
    failure = next(record for record in caplog.records if "last committed cursor" in record.message)
    assert failure.summary["reconcile_after_id"] == 1
    assert failure.summary["reconciled_rows_updated"] == 1
    with sqlite_engine.begin() as connection:
        assert connection.execute(
            text("SELECT source_prow_job_id FROM ci_l1_pod_lifecycle ORDER BY id")
        ).scalars().all() == ["GCP", None]
    monkeypatch.setattr(pods, "_reconcile_lifecycle_batch", original)
    resumed = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine, **options, after_id=1, through_id=2
    )
    assert resumed.reconciled_rows_updated == 1 and resumed.reconcile_complete
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ci_l1_pod_lifecycle SET source_prow_job_id = NULL, normalized_build_url = NULL, repo_full_name = NULL, job_name = NULL, pod_author = NULL, pod_org = NULL, pod_repo = NULL, ci_job = NULL"
            )
        )
    monkeypatch.setattr(pods, "_reconcile_lifecycle_batch", fail_second)
    calls = 0
    with pytest.raises(RuntimeError, match="second batch"):
        with sqlite_engine.begin() as connection:
            pods._reconcile_lifecycle_rows_in_time_window(connection, **options)
    with sqlite_engine.begin() as connection:
        assert connection.execute(
            text("SELECT source_prow_job_id FROM ci_l1_pod_lifecycle")
        ).scalars().all() == [None, None]


def test_optimistic_missing_only_update_does_not_clobber_concurrent_watcher(
    sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as connection:
        _build(connection)
        _save(connection)
    original = pods._load_build_metadata_map

    def racing(connection, rows, **kwargs):
        result = original(connection, rows, **kwargs)
        connection.execute(
            text(
                "UPDATE ci_l1_pod_lifecycle SET source_prow_job_id = 'concurrent', normalized_build_url = :url, pod_author = 'watcher'"
            ),
            {"url": _url().replace("123", "124")},
        )
        return result

    monkeypatch.setattr(pods, "_load_build_metadata_map", racing)
    summary = pods.run_reconcile_pod_linkage_for_time_window(
        sqlite_engine,
        start_time_from=NOW - timedelta(days=1),
        start_time_to=NOW + timedelta(days=1),
    )
    assert summary.reconciled_rows_updated == 0
    assert summary.reconciled_rows_skipped == 1
    with sqlite_engine.begin() as connection:
        assert connection.execute(
            text("SELECT source_prow_job_id, pod_author, repo_full_name FROM ci_l1_pod_lifecycle")
        ).one() == ("concurrent", "watcher", None)


def test_recent_gcp_sweep_never_repairs_tencent(sqlite_engine, monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    with sqlite_engine.begin() as connection:
        _build(connection, cloud="TENCENT", start=now)
        _save(connection, source=TENCENT, scheduled=now)
        assert pods._reconcile_recent_lifecycle_rows(connection, batch_size=1) == 0
        assert (
            connection.execute(
                text("SELECT normalized_build_url FROM ci_l1_pod_lifecycle")
            ).scalar_one()
            is None
        )


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 0},
        {"batch_size": 1001},
        {"max_rows": 0},
        {"max_rows": 10001},
        {"after_id": -1},
        {"after_id": 2, "through_id": 1},
        {"source_project": "tencent:invalid"},
        {"start_time_to": NOW - timedelta(days=2)},
    ],
)
def test_reconciliation_rejects_unbounded_or_invalid_controls(sqlite_engine, options):
    with pytest.raises(ValueError):
        pods.run_reconcile_pod_linkage_for_time_window(
            sqlite_engine,
            **{
                "start_time_from": NOW - timedelta(days=1),
                "start_time_to": NOW + timedelta(days=1),
                **options,
            },
        )


@pytest.mark.parametrize(
    "payload,first,last",
    [
        (
            {
                "eventTime": "2026-04-21T10:00:00Z",
                "firstTimestamp": "2026-04-21T09:00:00Z",
                "lastTimestamp": "2026-04-21T11:00:00Z",
                "series": {"lastObservedTime": "2026-04-21T12:00:00Z"},
            },
            10,
            12,
        ),
        (
            {"firstTimestamp": "2026-04-21T10:00:00Z", "lastTimestamp": "2026-04-21T09:00:00Z"},
            10,
            10,
        ),
        (
            {
                "metadata": {"creationTimestamp": "2026-04-21T10:00:00Z"},
                "lastTimestamp": "2026-04-21T11:00:00Z",
            },
            10,
            11,
        ),
        ({"lastTimestamp": "2026-04-21T11:00:00Z"}, None, 11),
        ({"series": {"lastObservedTime": "2026-04-21T12:00:00+02:00"}}, None, 10),
        (
            {
                "eventTime": "invalid",
                "firstTimestamp": "2026-04-21T10:00:00Z",
                "series": {"lastObservedTime": "invalid"},
            },
            10,
            10,
        ),
        ({}, None, None),
    ],
)
def test_shared_event_timestamp_precedence(payload, first, last):
    assert pods._event_first_last_timestamps(payload) == (
        datetime(2026, 4, 21, first) if first else None,
        datetime(2026, 4, 21, last) if last else None,
    )


def _event(
    *, uid="uid", reason="Scheduled", index=0, event=NOW, first=None, last=None, message=None
):
    return pods.NormalizedPodEvent(
        GCP,
        "cluster",
        "region",
        "jenkins-tidb",
        "agent-123-new",
        uid,
        reason,
        "Normal",
        message,
        event,
        NOW,
        first,
        last,
        None,
        None,
        str(index),
    ).as_db_params()


@pytest.mark.parametrize("multi", [False, True])
def test_both_aggregate_paths_use_first_and_latest_evidence(sqlite_engine, multi):
    with sqlite_engine.begin() as connection:
        pods._upsert_pod_events(
            connection,
            [
                _event(
                    index=1,
                    event=NOW + timedelta(hours=2),
                    first=NOW,
                    last=NOW + timedelta(hours=1),
                ),
                _event(
                    index=2, reason="Pulling", event=NOW + timedelta(minutes=2)
                ),  # timestamp-only proxy
                _event(
                    index=3,
                    reason="Pulled",
                    event=NOW + timedelta(minutes=3),
                    last=NOW + timedelta(minutes=3),
                ),  # last-only isn't a first
                _event(
                    index=4, reason="FailedScheduling", first=NOW, last=NOW + timedelta(hours=3)
                ),
            ],
        )
        identities = [(GCP, "jenkins-tidb", "uid", "agent-123-new")]
        if multi:
            identities.append((GCP, "jenkins-tidb", "absent", "absent"))
        row = pods._load_lifecycle_aggregates(connection, identities)[0]
    assert pods._parse_datetime(row["scheduled_at"]) == NOW
    assert pods._parse_datetime(row["first_pulling_at"]) == NOW + timedelta(minutes=2)
    assert row["first_pulled_at"] is None
    assert pods._parse_datetime(row["last_event_at"]) == NOW + timedelta(hours=3)
    assert pods._parse_datetime(row["last_failed_scheduling_at"]) == NOW + timedelta(hours=3)
    assert row["failed_scheduling_count"] == 1  # stored versions, not series occurrences


def test_logging_legacy_index_time_and_series_recency_and_replay(sqlite_engine):
    entry = dict(
        logName="projects/gcp-project/logs/events",
        insertId="version",
        timestamp="2026-04-21T14:00:00Z",
        jsonPayload={
            "reason": "Unhealthy",
            "firstTimestamp": "2026-04-21T10:00:00Z",
            "lastTimestamp": "2026-04-21T11:00:00Z",
            "series": {"lastObservedTime": "2026-04-21T15:00:00Z"},
        },
    )
    row = pods._normalize_logging_entry(entry)
    assert row.event_timestamp == datetime(2026, 4, 21, 14)
    assert row.first_timestamp == datetime(2026, 4, 21, 10)
    assert row.last_timestamp == datetime(2026, 4, 21, 15)
    del entry["timestamp"]
    assert pods._normalize_logging_entry(entry).event_timestamp == datetime(2026, 4, 21, 11)
    with sqlite_engine.begin() as connection:
        pods._upsert_pod_events(connection, [row.as_db_params()])
        pods._upsert_pod_events(connection, [pods._normalize_logging_entry(entry).as_db_params()])
        assert pods._parse_datetime(
            connection.execute(text("SELECT event_timestamp FROM ci_l1_pod_events")).scalar_one()
        ) == datetime(2026, 4, 21, 14)
        pods._upsert_pod_events(
            connection,
            [
                _event(
                    index=1,
                    reason="Unhealthy",
                    event=NOW,
                    last=NOW + timedelta(hours=3),
                    message="series latest",
                ),
                _event(
                    index=2,
                    reason="Unhealthy",
                    event=NOW + timedelta(hours=2),
                    message="envelope later",
                ),
            ],
        )
        identity = (GCP, "jenkins-tidb", "uid", "agent-123-new")
        assert (
            pods._load_abnormal_event_summaries(connection, [identity])[identity][
                "abnormal_message"
            ]
            == "series latest"
        )


def test_event_only_after_raw_retention_preserves_snapshot_and_extrema(sqlite_engine):
    identity = (GCP, "jenkins-tidb", "uid", "agent-123-new")
    with sqlite_engine.begin() as connection:
        _save(
            connection,
            created=NOW - timedelta(minutes=1),
            annotations={"runUrl": PATH},
            pod_labels_json='{"author":"watcher"}',
            pod_author="watcher",
            pod_org="pingcap",
            first_started_at=NOW + timedelta(seconds=10),
            last_failed_scheduling_at=NOW + timedelta(hours=3),
            last_event_at=NOW + timedelta(hours=3),
            failed_scheduling_count=5,
            abnormal_reason="Evicted",
            abnormal_message="stored diagnosis",
        )
        # Late Logging Event, after Pod deletion and raw event retention.
        pods._upsert_pod_events(
            connection,
            [
                _event(
                    index=1,
                    event=NOW + timedelta(minutes=2),
                    first=NOW + timedelta(minutes=1),
                    last=NOW + timedelta(minutes=2),
                )
            ],
        )
        rows = pods._build_lifecycle_rows(connection, [identity], pod_metadata_by_identity={})
        pods._upsert_pod_lifecycle(connection, rows)
        row = connection.execute(text("SELECT * FROM ci_l1_pod_lifecycle")).mappings().one()
    assert pods._parse_datetime(row["scheduled_at"]) == NOW
    assert pods._parse_datetime(row["first_started_at"]) == NOW + timedelta(seconds=10)
    assert pods._parse_datetime(row["last_failed_scheduling_at"]) == NOW + timedelta(hours=3)
    assert row["failed_scheduling_count"] == 5
    assert row["schedule_to_started_seconds"] == 10
    assert row["pod_author"] == "watcher" and row["pod_org"] == "pingcap"
    assert row["pod_created_at"] and row["pod_labels_json"] and row["pod_annotations_json"]
    assert row["abnormal_reason"] == "Evicted" and row["abnormal_message"] == "stored diagnosis"


def test_upsert_is_race_safe_for_absent_stale_and_real_empty_snapshots(sqlite_engine):
    with sqlite_engine.begin() as connection:
        initial = _save(
            connection,
            created=NOW - timedelta(minutes=1),
            annotations={"runUrl": PATH},
            pod_labels_json='{"author":"old"}',
            pod_author="old",
        )
        absent = {
            **initial,
            **pods._empty_pod_metadata_fields(),
            "scheduled_at": NOW + timedelta(hours=1),
        }
        pods._upsert_pod_lifecycle(connection, [absent])
        stored = connection.execute(text("SELECT * FROM ci_l1_pod_lifecycle")).mappings().one()
        assert stored["pod_author"] == "old"
        assert stored["pod_created_at"] and stored["metadata_observed_at"]
        newer_empty = {
            **initial,
            **_metadata(created=None, observed=NOW + timedelta(hours=1)).as_lifecycle_fields(),
        }
        pods._upsert_pod_lifecycle(connection, [newer_empty])
        pods._upsert_pod_lifecycle(
            connection, [initial]
        )  # stale nonempty snapshot can't resurrect labels
        stored = connection.execute(text("SELECT * FROM ci_l1_pod_lifecycle")).mappings().one()
        assert stored["pod_author"] is None and stored["pod_labels_json"] is None
        assert stored["pod_annotations_json"] is None
        assert pods._parse_datetime(stored["metadata_observed_at"]) == NOW + timedelta(hours=1)
        assert stored["pod_created_at"]


def test_racing_upsert_never_pairs_preserved_link_with_other_build_fields(sqlite_engine):
    with sqlite_engine.begin() as connection:
        original = _save(
            connection,
            normalized_build_url=_url(),
            source_prow_job_id="existing",
            repo_full_name=None,
            job_name=None,
        )
        incoming = {
            **original,
            "normalized_build_url": _url().replace("123", "124"),
            "source_prow_job_id": "new",
            "repo_full_name": "wrong/repo",
            "job_name": "wrong/job",
            "pod_author": "wrong",
            "pod_org": "wrong",
            "pod_repo": "wrong",
            "ci_job": "wrong/job",
            "metadata_observed_at": NOW + timedelta(hours=1),
        }
        pods._upsert_pod_lifecycle(connection, [incoming])
        stored = connection.execute(
            text(
                "SELECT normalized_build_url, source_prow_job_id, repo_full_name, job_name, pod_author, pod_org, pod_repo, ci_job FROM ci_l1_pod_lifecycle"
            )
        ).one()
        assert stored == (_url(), "existing", None, None, None, None, None, None)
        _build(connection)
        # Fresh metadata-only watcher rows must consult the saved populated link too.
        row = _row()
        row["scheduled_at"] = None
        result = _resolve(connection, row, _metadata(annotations={"runUrl": PATH}))
        assert result["attribution_reason"] == "contradictory_existing_link"


@pytest.mark.parametrize(
    "scheduled,started,expected",
    [
        ("2026-04-21T10:00:00Z", datetime(2026, 4, 21, 10, 0, 10), 10),
        (datetime(2026, 4, 21, 10, 0, 10), "2026-04-21T10:00:00Z", None),
    ],
)
def test_sqlite_duration_guard_compares_instants_not_timestamp_formats(
    sqlite_engine, scheduled, started, expected
):
    with sqlite_engine.begin() as connection:
        initial = _save(connection, scheduled=scheduled)
        pods._upsert_pod_lifecycle(connection, [{**initial, "first_started_at": started}])
        assert (
            connection.execute(
                text("SELECT schedule_to_started_seconds FROM ci_l1_pod_lifecycle")
            ).scalar_one()
            == expected
        )


def test_saved_snapshot_without_observation_time_never_fabricates_provenance(sqlite_engine):
    with sqlite_engine.begin() as connection:
        initial = _save(
            connection,
            pod_labels_json='{"author":"legacy"}',
            pod_author="legacy",
            metadata_observed_at=None,
        )
        saved = connection.execute(text("SELECT * FROM ci_l1_pod_lifecycle")).mappings().one()
        metadata = pods._deserialize_pod_metadata_snapshots_from_lifecycle_rows([saved])
        assert metadata[_identity(initial)].observed_at is None
        newer_empty = {**initial, **_metadata(observed=NOW).as_lifecycle_fields()}
        pods._upsert_pod_lifecycle(connection, [newer_empty])
        pods._upsert_pod_lifecycle(connection, [initial])
        saved = (
            connection.execute(
                text(
                    "SELECT pod_labels_json, pod_author, metadata_observed_at FROM ci_l1_pod_lifecycle"
                )
            )
            .mappings()
            .one()
        )
        assert saved["pod_labels_json"] is None and saved["pod_author"] is None
        assert pods._parse_datetime(saved["metadata_observed_at"]) == NOW


def test_negative_duration_and_mysql_assignment_semantics(sqlite_engine):
    with sqlite_engine.begin() as connection:
        initial = _save(connection, first_started_at=NOW - timedelta(seconds=1))
        pods._upsert_pod_lifecycle(connection, [initial])
        assert (
            connection.execute(
                text("SELECT schedule_to_started_seconds FROM ci_l1_pod_lifecycle")
            ).scalar_one()
            is None
        )
    sql = str(
        pods._build_pod_lifecycle_upsert_statement(
            SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
        )
    )
    assert "ON DUPLICATE KEY UPDATE" in sql and "VALUES(pod_created_at)" in sql
    assert "TIMESTAMPDIFF(SECOND" in sql and "COALESCE(ci_l1_pod_lifecycle.pod_created_at" in sql
    updates = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert updates.index("pod_labels_json =") < updates.index("metadata_observed_at =")
    assert updates.index("\npod_author =") < updates.index("\nsource_prow_job_id =")
    assert updates.index("\nrepo_full_name =") < updates.index("\nnormalized_build_url =")
    assert "ci_l1_pod_lifecycle.normalized_build_url = VALUES(normalized_build_url)" in sql
