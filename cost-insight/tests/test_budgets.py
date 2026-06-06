import pytest

from cost_insight.budgets import build_filter_hash, canonicalize_label_filters


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


def test_canonicalize_label_filters_rejects_unsupported_values() -> None:
    with pytest.raises(ValueError, match="Unsupported label filter value"):
        canonicalize_label_filters(object())
