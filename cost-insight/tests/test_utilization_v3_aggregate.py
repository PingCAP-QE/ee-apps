"""Offline V3 statistics and migration contracts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from cost_insight.utilization.aggregate import summarize_window, validate_stats
from cost_insight.utilization.schema import (
    MACHINE_STATS,
    TABLES,
    WORKLOAD_STATS,
    contexts,
    workloads,
)

START = datetime(2026, 11, 20, 4, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
END = START + 4 * MINUTE
MACHINE = {"kind": "machine", "machine_id": "ins-historical"}
POD = {"kind": "pod", "pod_uid": "uid", "cpu_request_cores": 4, "memory_request_bytes": 4096}


def point(target, metric, offset, value):
    machine = target["kind"] == "machine"
    return {
        "entity": target["machine_id" if machine else "pod_uid"],
        "metric": metric,
        "timestamp": START + offset * MINUTE,
        "value": value,
        "unit": "percent" if machine else "absolute",
    }


def test_machine_percentages_without_specs_keep_zero_and_deduplicate():
    cpu = point(MACHINE, "cpu", 0, 50)
    points = [
        cpu,
        dict(cpu),
        point(MACHINE, "cpu", 1, 25),
        point(MACHINE, "cpu", 2, None),
        point(MACHINE, "memory", 3, 0),
        point(MACHINE, "cpu", 4, 100),  # exclusive right endpoint
        point(MACHINE, "memory", -1, 100),
    ]
    stats = summarize_window(points, MACHINE, START, END)
    assert stats == {
        "cpu_percent_sum": 75,
        "cpu_sample_minutes": 2,
        "cpu_peak_percent": 50,
        "memory_percent_sum": 0,
        "memory_sample_minutes": 1,
        "memory_peak_percent": 0,
    }
    assert set(stats) == set(MACHINE_STATS)


def test_empty_metrics_are_zero_counts_and_null_peaks():
    stats = summarize_window([], MACHINE, START, END)
    assert stats == {
        "cpu_percent_sum": 0,
        "cpu_sample_minutes": 0,
        "cpu_peak_percent": None,
        "memory_percent_sum": 0,
        "memory_sample_minutes": 0,
        "memory_peak_percent": None,
    }


def test_pod_usage_and_requests_integrate_only_independently_matched_minutes():
    points = [
        point(POD, "cpu", 0, 2),
        point(POD, "cpu", 1, 0),
        point(POD, "memory", 1, None),
        point(POD, "memory", 2, 1024),
    ]
    stats = summarize_window(points, POD, START, END)
    assert stats == {
        "cpu_used": 120,
        "cpu_requested": 480,
        "memory_used": 61_440,
        "memory_requested": 245_760,
        "cpu_matched_pod_minutes": 2,
        "memory_matched_pod_minutes": 1,
        "expected_pod_minutes": 4,
    }
    assert set(stats) == set(WORKLOAD_STATS)


@pytest.mark.parametrize("request_cores", [None, 0])
def test_pod_observed_usage_with_unknown_or_zero_request_is_unmatched(request_cores):
    target = {**POD, "cpu_request_cores": request_cores}
    stats = summarize_window(
        [point(target, "cpu", 0, 2), point(target, "memory", 0, 0)], target, START, END
    )
    assert stats == {
        "cpu_used": 0,
        "cpu_requested": 0,
        "memory_used": 0,
        "memory_requested": 245_760,
        "cpu_matched_pod_minutes": 0,
        "memory_matched_pod_minutes": 1,
        "expected_pod_minutes": 4,
    }
    assert summarize_window([], target, START, END) == {
        key: 4 if key == "expected_pod_minutes" else 0 for key in WORKLOAD_STATS
    }


@pytest.mark.parametrize(
    "target, overrides, message",
    [
        (MACHINE, {"unit": "absolute"}, "source unit"),
        (MACHINE, {"value": 100.01}, "outside 0..100"),
        (MACHINE, {"value": -1}, "finite nonnegative"),
        (MACHINE, {"value": float("nan")}, "finite nonnegative"),
        (POD, {"unit": "percent"}, "source unit"),
        (POD, {"entity": "another-pod"}, "identity or alignment"),
        (POD, {"timestamp": START + timedelta(seconds=1)}, "identity or alignment"),
        (POD, {"metric": "disk"}, "identity or alignment"),
    ],
)
def test_invalid_points_are_rejected(target, overrides, message):
    with pytest.raises(ValueError, match=message):
        summarize_window([{**point(target, "cpu", 0, 5), **overrides}], target, START, END)


def test_conflicting_duplicate_points_are_rejected():
    with pytest.raises(ValueError, match="conflicting duplicate"):
        summarize_window(
            [point(MACHINE, "cpu", 0, 50), point(MACHINE, "cpu", 0, 25)], MACHINE, START, END
        )


@pytest.mark.parametrize("end", [START, START + timedelta(seconds=30), START + 31 * MINUTE])
def test_invalid_window_is_rejected(end):
    with pytest.raises(ValueError, match="completed source minutes"):
        summarize_window([], MACHINE, START, end)


@pytest.mark.parametrize(
    "target, overrides",
    [
        (MACHINE, {"cpu_peak_percent": 0}),
        (MACHINE, {"cpu_sample_minutes": 1, "cpu_peak_percent": 90, "cpu_percent_sum": 95}),
        (MACHINE, {"cpu_sample_minutes": False}),
        (POD, {"cpu_matched_pod_minutes": 5, "cpu_requested": 1200}),
        (POD, {"cpu_matched_pod_minutes": 1}),
        (POD, {"memory_used": 1}),
        (POD, {"expected_pod_minutes": 4.0}),
    ],
)
def test_invalid_sufficient_statistics_are_rejected(target, overrides):
    stats = summarize_window([], target, START, END)
    with pytest.raises(ValueError):
        validate_stats({**stats, **overrides}, machine=target["kind"] == "machine")


def test_offline_mysql_ddl_matches_metadata():
    assert {table.name: len(table.c) for table in TABLES} == {
        "machine_utilization_daily": 13,
        "workload_utilization_daily": 11,
        "pod_utilization_context": 16,
        "utilization_task": 13,
    }
    assert contexts.c.node_instance_id.nullable
    assert list(workloads.primary_key.columns.keys()) == ["account_id", "usage_date", "group_key"]
    sql = (Path(__file__).resolve().parents[1] / "sql/031_create_utilization.sql").read_text()
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    dialect = mysql.dialect()
    compiled = [
        str(ddl.compile(dialect=dialect)).replace(")CHARSET=", ") CHARACTER SET ")
        for table in TABLES
        for ddl in [
            CreateTable(table, if_not_exists=True),
            *(CreateIndex(i) for i in table.indexes),
        ]
    ]
    assert sorted(" ".join(stmt.split()) for stmt in compiled) == sorted(
        " ".join(stmt.split()) for stmt in sql.split(";") if stmt.strip()
    )
