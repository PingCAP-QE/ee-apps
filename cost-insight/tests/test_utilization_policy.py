"""V3 numeric and Beijing calendar contracts."""
# ruff: noqa: DTZ001
# The persistence contract deliberately uses UTC-naive business timestamps.

from datetime import datetime, timedelta

import pytest

from cost_insight.utilization import policy
from cost_insight.utilization.aggregate import summarize_window

START = datetime(2026, 11, 25, 4)
END = START + timedelta(minutes=10)


@pytest.mark.parametrize("capacity", [None, 0, 4])
def test_only_matched_positive_requests_retain_usage(capacity):
    target = {
        "kind": "pod",
        "pod_uid": "uid",
        "cpu_request_cores": capacity,
        "memory_request_bytes": 1024,
    }
    points = [
        {"entity": "uid", "metric": "cpu", "timestamp": START, "value": 2, "unit": "absolute"}
    ]
    result = summarize_window(points, target, START, END)
    assert result["cpu_used"] == (120 if capacity == 4 else 0)
    assert result["cpu_requested"] == (240 if capacity == 4 else 0)
    assert result["cpu_matched_pod_minutes"] == (1 if capacity == 4 else 0)
    assert result["expected_pod_minutes"] == 10
    assert result["memory_matched_pod_minutes"] == 0
    assert not any("peak" in key for key in result)


def test_machine_percentage_needs_no_capacity_and_preserves_missing_vs_zero():
    target = {"kind": "machine", "machine_id": "ins-a"}
    result = summarize_window(
        [{"entity": "ins-a", "metric": "cpu", "timestamp": START, "value": 0, "unit": "percent"}],
        target,
        START,
        END,
    )
    assert result["cpu_percent_sum"] == 0 and result["cpu_sample_minutes"] == 1
    assert result["cpu_peak_percent"] == 0
    assert result["memory_sample_minutes"] == 0 and result["memory_peak_percent"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified_horizon_seconds", 0),
        ("verified_horizon_seconds", float("inf")),
        ("safety_margin_seconds", -1),
        ("safety_margin_seconds", 100),
        ("timestamp_alignment_verified", False),
    ],
)
def test_invalid_source_window_rejected(field, value):
    entry = {
        "verified_horizon_seconds": 100,
        "safety_margin_seconds": 10,
        "timestamp_alignment_verified": True,
    }
    entry[field] = value
    with pytest.raises(ValueError):
        policy.validate_source_window(entry)


def test_week_expiry_only_for_short_lived_workload_not_machine_schema():
    assert policy.expires_at(datetime(2026, 11, 20, 4)) == datetime(2026, 11, 29, 16)
    assert policy.usage_date(datetime(2026, 11, 20, 16)).isoformat() == "2026-11-21"
    assert policy.workload_start_date("2026-11-29T15:59:59+00:00").isoformat() == "2026-11-16"
    assert policy.workload_start_date("2026-11-29T16:00:00+00:00").isoformat() == "2026-11-23"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_quantity_rejected(value):
    with pytest.raises(ValueError):
        policy.finite(value)


def test_conflicting_points_and_wrong_identity_rejected():
    target = {"kind": "machine", "machine_id": "ins-a", "cpu_cores": 2, "memory_bytes": 1024}
    point = {"entity": "ins-a", "metric": "cpu", "timestamp": START, "value": 50, "unit": "percent"}
    with pytest.raises(ValueError, match="duplicate"):
        summarize_window([point, {**point, "value": 60}], target, START, END)
    with pytest.raises(ValueError, match="identity"):
        summarize_window([{**point, "entity": "ins-b"}], target, START, END)
