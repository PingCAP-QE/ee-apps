from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def build_filter_hash(label_filters: Mapping[str, Any] | None) -> str:
    canonical = canonicalize_label_filters(label_filters)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonicalize_label_filters(label_filters: Any) -> Any:
    if label_filters is None:
        return None
    if isinstance(label_filters, Mapping):
        return {
            str(key): canonicalize_label_filters(label_filters[key])
            for key in sorted(label_filters, key=str)
        }
    if isinstance(label_filters, set):
        return _sort_values(canonicalize_label_filters(value) for value in label_filters)
    if isinstance(label_filters, Sequence) and not isinstance(label_filters, str | bytes | bytearray):
        return _sort_values(canonicalize_label_filters(value) for value in label_filters)
    if isinstance(label_filters, str | int | float | bool):
        return label_filters
    raise ValueError(f"Unsupported label filter value: {label_filters!r}")


def _sort_values(values) -> list[Any]:
    return sorted(
        values,
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
