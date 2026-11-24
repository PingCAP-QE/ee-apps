# ruff: noqa: DTZ001 -- DB contract deliberately uses UTC-naive timestamps.
from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest

from cost_insight.utilization import policy

NOW = datetime(2026, 11, 18, 4)


def state():
    return {
        "version": 1,
        "enabled": True,
        "counts_reliable": True,
        "reconciled_at": NOW.isoformat(),
        "maintenance_at": NOW.isoformat(),
        "observed_bytes": 100,
        "bytes_observed_at": NOW.isoformat(),
        "retained_rows": 10,
        "oldest_expired_at": None,
        "paused": False,
        "collection_start_week": "2026-11-16",
        "collection_stop_week": None,
        "finalized_through_week": None,
    }


def contract(denominator="physical"):
    return {
        "namespace": "QCE/CVM",
        "metric": "CpuUsage",
        "period": 60,
        "source_stat": "avg",
        "unit": "percent",
        "normalized_unit": "cores",
        "semantic_version": "v1",
        "denominator": denominator,
    }


def test_beijing_boundaries_and_fixed_expiry():
    monday = date(2026, 11, 16)
    sunday = monday + timedelta(days=6)
    assert policy.week_start(datetime(2026, 11, 15, 15, 59)) == monday - timedelta(days=7)
    assert policy.week_start(datetime(2026, 11, 15, 16)) == monday
    assert policy.week_start(sunday) == monday
    assert policy.expires_at(monday) == datetime(2026, 11, 29, 16)
    assert policy.expires_at(monday) - policy.day_start(monday) == timedelta(days=14)
    assert policy.expires_at(sunday) - policy.day_start(sunday) == timedelta(days=8)
    assert policy.expires_at(monday) - policy.day_start(monday + timedelta(days=1)) == timedelta(
        days=13
    )
    assert policy.expires_at(sunday) - policy.day_start(sunday + timedelta(days=1)) == timedelta(
        days=7
    )
    assert policy.utc_naive("2026-11-16T00:00:00+08:00") == policy.day_start(monday)
    assert policy.utc_naive("2026-11-15T16:00:00Z") == policy.day_start(monday)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("enabled", False, "disabled_or_unconfigured"),
        ("counts_reliable", False, "counts_unreliable"),
        ("reconciled_at", None, "counts_stale"),
        ("reconciled_at", (NOW - timedelta(hours=27)).isoformat(), "counts_stale"),
        ("maintenance_at", None, "maintenance_uninitialized"),
        ("maintenance_at", (NOW - timedelta(hours=5)).isoformat(), "maintenance_stale"),
        ("maintenance_at", (NOW + timedelta(minutes=2)).isoformat(), "maintenance_stale"),
        ("oldest_expired_at", (NOW - timedelta(hours=5)).isoformat(), "expiry_overdue"),
        ("retained_rows", -1, "counts_unreliable"),
        ("retained_rows", 2_000_000, "row_budget"),
        ("observed_bytes", 8 * 1024**3, "byte_budget"),
        ("observed_bytes", None, "bytes_unreliable"),
        ("bytes_observed_at", None, "bytes_unreliable"),
        ("bytes_observed_at", (NOW - timedelta(hours=5)).isoformat(), "bytes_stale"),
        ("paused", True, "paused"),
    ],
)
def test_health_failclosed(field, value, reason):
    payload = state()
    payload[field] = value
    result = policy.health(payload, NOW)
    assert not result["allowed"]
    assert reason in result["reasons"]


@pytest.mark.parametrize(
    ("field", "value", "warning"),
    [
        ("retained_rows", 1_000_000, "row_budget"),
        ("observed_bytes", 4 * 1024**3, "byte_budget"),
        ("maintenance_at", (NOW - timedelta(hours=2)).isoformat(), "maintenance_stale"),
        ("oldest_expired_at", (NOW - timedelta(hours=3)).isoformat(), "expiry_overdue"),
    ],
)
def test_warning_thresholds(field, value, warning):
    payload = state()
    payload[field] = value
    result = policy.health(payload, NOW)
    assert result["allowed"]
    assert warning in result["warnings"]
    assert not policy.health(payload, NOW, projected_rows=2_000_000)["allowed"]


def test_fence_and_calendar():
    payload = state()
    week = date(2026, 11, 16)
    assert policy.writable_week(payload, week, NOW)
    assert not policy.writable_week(payload, week - timedelta(days=7), NOW)
    payload["collection_start_week"] = "2026-11-09"
    assert policy.writable_week(payload, week - timedelta(days=7), NOW)
    payload["finalized_through_week"] = "2026-11-09"
    assert not policy.writable_week(payload, week - timedelta(days=7), NOW)
    payload["collection_stop_week"] = "2026-11-09"
    assert not policy.writable_week(payload, week, NOW)
    assert not policy.writable_week(state(), week, policy.expires_at(week))


def test_contracts_and_whole_day_horizon():
    physical, requests = contract(), contract("requests")
    contracts = {"cvm": {"cpu": {"contract": physical, "scope_contracts": {"job": requests}}}}
    assert policy.expected_contract(contracts, "cvm", "cpu") == physical
    assert policy.expected_contract(contracts, "cvm", "cpu", "job") == requests
    policy.validate_contract(physical, "cpu", "account_class")
    policy.validate_contract(requests, "cpu", "job")
    for field, value in (
        ("period", 300),
        ("source_stat", "max"),
        ("denominator", "allocatable"),
        ("normalized_unit", "bytes"),
    ):
        bad = deepcopy(physical)
        bad[field] = value
        with pytest.raises(ValueError):
            policy.validate_contract(bad, "cpu", "resource")
    with pytest.raises(ValueError):
        policy.validate_contract({}, "disk", "resource")
    day = date(2026, 11, 16)
    assert not policy.replay_allowed(day, {}, NOW)
    assert not policy.replay_allowed(
        day, {"verified_horizon_seconds": 86400, "timestamp_alignment_verified": True}, NOW
    )
    assert policy.replay_allowed(
        day, {"verified_horizon_seconds": 3 * 86400, "timestamp_alignment_verified": True}, NOW
    )
    assert not policy.replay_allowed(
        day,
        {
            "verified_horizon_seconds": 3 * 86400,
            "safety_margin_seconds": 86400,
            "timestamp_alignment_verified": True,
        },
        NOW,
    )
    day = date(2026, 11, 16)
    assert not policy.replay_allowed(
        day,
        {"verified_horizon_seconds": 30 * 86400, "timestamp_alignment_verified": True},
        policy.expires_at(day),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified_horizon_seconds", value)
        for value in (0, -1, float("nan"), float("inf"), "86400", None, True)
    ]
    + [
        ("safety_margin_seconds", value)
        for value in (-1, 3 * 86400, 4 * 86400, float("nan"), float("inf"), "0", None, True)
    ]
    + [("timestamp_alignment_verified", False), ("timestamp_alignment_verified", None)],
)
def test_invalid_source_windows_fail_configuration_and_replay(field, value):
    entry = {
        "verified_horizon_seconds": 3 * 86400,
        "safety_margin_seconds": 60,
        "timestamp_alignment_verified": True,
    }
    entry[field] = value
    with pytest.raises(ValueError):
        policy.validate_source_window(entry)
    assert not policy.replay_allowed(date(2026, 11, 16), entry, NOW)


@pytest.mark.parametrize("scale", [0, -1, float("nan"), float("inf"), "1", None, True])
def test_source_scale_rejects_nonpositive_or_unverified_values(scale):
    source = dict(contract(), source_scale=scale)
    with pytest.raises(ValueError, match="source_scale"):
        policy.validate_contract(source, "cpu", "account_class")


def test_source_window_requires_alignment_and_respects_boundary():
    entry = {"verified_horizon_seconds": 3 * 86400, "safety_margin_seconds": 0}
    with pytest.raises(ValueError, match="timestamp alignment"):
        policy.validate_source_window(entry)
    entry["timestamp_alignment_verified"] = True
    policy.validate_source_window(entry)
    day = date(2026, 11, 16)
    edge = policy.day_start(day) + timedelta(days=3)
    assert policy.replay_allowed(day, entry, edge)
    assert not policy.replay_allowed(day, entry, edge + timedelta(microseconds=1))
    policy.validate_contract(dict(contract(), source_scale=1), "cpu", "account_class")
