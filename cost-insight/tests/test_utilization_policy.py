"""V2 numeric/calendar contracts; no fixed-week machine retention or group peaks."""
# ruff: noqa: DTZ001
# The persistence contract deliberately uses UTC-naive business timestamps.

from datetime import datetime, timedelta

import pytest

from cost_insight.utilization import policy
from cost_insight.utilization.aggregate import summarize_window

START = datetime(2026, 11, 25, 4)
END = START + timedelta(minutes=10)


@pytest.mark.parametrize("capacity", [None, 0, 4])
def test_unknown_zero_and_positive_requests_keep_absolute_usage(capacity):
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
    assert result["cpu_total_used"] == 120 and result["cpu_usage_seconds"] == 60
    assert result["cpu_capacity"] == (240 if capacity == 4 else 0)
    assert result["cpu_observed_seconds"] == (60 if capacity == 4 else 0)
    assert result["cpu_unknown_seconds"] == (600 if capacity is None else 0)
    assert result["cpu_zero_seconds"] == (600 if capacity == 0 else 0)
    assert not any("peak" in key for key in result)


def test_machine_percentage_is_converted_with_its_own_capacity():
    target = {"kind": "machine", "machine_id": "ins-a", "cpu_cores": 2, "memory_bytes": 1024}
    result = summarize_window(
        [{"entity": "ins-a", "metric": "cpu", "timestamp": START, "value": 50, "unit": "percent"}],
        target,
        START,
        END,
    )
    assert result["cpu_used"] == 60 and result["cpu_capacity"] == 120
    assert result["cpu_peak"] == 50 and result["cpu_peak_at"] == START
    assert result["cpu_expected_capacity"] == 1200


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
