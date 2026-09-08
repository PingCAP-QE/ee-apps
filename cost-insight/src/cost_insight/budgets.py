from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def build_filter_hash(label_filters: Mapping[str, Any] | None) -> str:
    canonical = canonicalize_label_filters(label_filters)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_scope_key(
    *,
    vendor: str | None = None,
    account_id: str | None = None,
    label_filters: Mapping[str, Any] | None = None,
) -> str:
    if vendor is not None or account_id is not None:
        if not vendor or not account_id or label_filters is not None:
            raise ValueError("Account budgets require vendor/account_id and no label filters")
        identity = f"account\0{vendor}\0{account_id}"
    else:
        filters = canonicalize_label_filters(label_filters)
        projects = filters.get("project") if isinstance(filters, Mapping) else None
        if (
            not isinstance(projects, list)
            or set(filters) != {"project"}
            or not projects
            or any(not isinstance(project, str) or not project.strip() for project in projects)
            or len(set(projects)) != len(projects)
        ):
            raise ValueError("Project-set budgets require one non-empty project filter")
        identity = f"project_set\0{build_filter_hash(filters)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
