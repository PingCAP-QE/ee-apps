import hashlib

import pytest

from cost_insight.budgets import build_filter_hash, build_scope_key, canonicalize_label_filters


def test_build_filter_hash_is_order_independent() -> None:
    first = build_filter_hash(
        {
            "repo": ["ticdc", "tidb"],
            "author": "dillon",
            "labels": {"b": "2", "a": "1"},
        }
    )
    second = build_filter_hash(
        {
            "labels": {"a": "1", "b": "2"},
            "author": "dillon",
            "repo": ["tidb", "ticdc"],
        }
    )

    assert first == second


def test_canonicalize_label_filters_keeps_null_scope_stable() -> None:
    assert canonicalize_label_filters(None) is None
    assert len(build_filter_hash(None)) == 64
    assert canonicalize_label_filters({"project": {"beta", "alpha"}}) == {
        "project": ["alpha", "beta"]
    }


def test_build_scope_key_is_stable_for_accounts_and_project_sets() -> None:
    account_scope = build_scope_key(vendor="aws", account_id="946646677266")
    assert account_scope == build_scope_key(vendor="aws", account_id="946646677266")
    assert account_scope == hashlib.sha256(
        b"account\x00aws\x00946646677266"
    ).hexdigest()
    assert build_scope_key(label_filters={"project": ["beta", "alpha"]}) == build_scope_key(
        label_filters={"project": ["alpha", "beta"]}
    )


def test_build_scope_key_rejects_mixed_or_arbitrary_scopes() -> None:
    with pytest.raises(ValueError, match="Account budgets"):
        build_scope_key(vendor="aws", account_id="1", label_filters={"project": ["qa"]})
    with pytest.raises(ValueError, match="Project-set budgets"):
        build_scope_key(label_filters={"repo": ["tidb"]})
    with pytest.raises(ValueError, match="Project-set budgets"):
        build_scope_key(label_filters={"project": ["qa", "qa"]})


def test_canonicalize_label_filters_rejects_unsupported_values() -> None:
    with pytest.raises(ValueError, match="Unsupported label filter value"):
        canonicalize_label_filters(object())
