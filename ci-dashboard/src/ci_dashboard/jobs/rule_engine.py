from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from ci_dashboard.common.models import ErrorClassification


def default_taxonomy_path() -> Path:
    resource = files("ci_dashboard.jobs").joinpath("error_taxonomy.json")
    return Path(str(resource))


@dataclass(frozen=True)
class CompiledRule:
    name: str
    l1_category: str
    l2_subcategory: str
    text_patterns: tuple[re.Pattern[str], ...]
    job_name_patterns: tuple[re.Pattern[str], ...]
    url_patterns: tuple[re.Pattern[str], ...]
    build_field_patterns: Mapping[str, tuple[re.Pattern[str], ...]]

    def matches(self, *, text: str, job_name: str, url: str, build: Mapping[str, Any]) -> bool:
        return (
            _matches_any(self.text_patterns, text)
            and _matches_any(self.job_name_patterns, job_name)
            and _matches_any(self.url_patterns, url)
            and _matches_build_fields(self.build_field_patterns, build)
        )


@dataclass(frozen=True)
class LoadedTaxonomy:
    default_l1_category: str
    default_l2_subcategory: str
    rules: tuple[CompiledRule, ...]


class RuleEngine:
    def __init__(self, taxonomy: LoadedTaxonomy) -> None:
        self._taxonomy = taxonomy

    @property
    def default_classification(self) -> ErrorClassification:
        return ErrorClassification(
            l1_category=self._taxonomy.default_l1_category,
            l2_subcategory=self._taxonomy.default_l2_subcategory,
            source="default",
        )

    @property
    def allowed_classifications(self) -> tuple[tuple[str, str], ...]:
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for rule in self._taxonomy.rules:
            candidate = (rule.l1_category, rule.l2_subcategory)
            if candidate in seen:
                continue
            seen.add(candidate)
            pairs.append(candidate)
        default_candidate = (
            self._taxonomy.default_l1_category,
            self._taxonomy.default_l2_subcategory,
        )
        if default_candidate not in seen:
            pairs.append(default_candidate)
        return tuple(pairs)

    def classify(
        self,
        *,
        log_text: str,
        build: Mapping[str, Any] | None = None,
    ) -> ErrorClassification | None:
        job_name = str((build or {}).get("job_name") or "")
        url = str((build or {}).get("url") or "")
        build_fields = build or {}
        for rule in self._taxonomy.rules:
            if rule.matches(text=log_text, job_name=job_name, url=url, build=build_fields):
                return ErrorClassification(
                    l1_category=rule.l1_category,
                    l2_subcategory=rule.l2_subcategory,
                    source=f"rule:{rule.name}",
                )
        return None

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> RuleEngine:
        resolved_path = Path(path) if path is not None else default_taxonomy_path()
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        return cls(load_taxonomy(payload))


def load_taxonomy(payload: Mapping[str, Any]) -> LoadedTaxonomy:
    default_payload = payload.get("default_classification") or {}
    default_l1 = _normalize_category(default_payload.get("l1"), field_name="default l1")
    default_l2 = _normalize_category(default_payload.get("l2"), field_name="default l2")
    compiled_rules: list[CompiledRule] = []
    for index, raw_rule in enumerate(payload.get("rules") or []):
        compiled_rules.append(_compile_rule(raw_rule, index=index))
    return LoadedTaxonomy(
        default_l1_category=default_l1,
        default_l2_subcategory=default_l2,
        rules=tuple(compiled_rules),
    )


def _compile_rule(raw_rule: Mapping[str, Any], *, index: int) -> CompiledRule:
    name = str(raw_rule.get("name") or f"rule_{index}").strip()
    if not name:
        raise ValueError(f"rule {index} is missing a name")
    return CompiledRule(
        name=name,
        l1_category=_normalize_category(raw_rule.get("l1"), field_name=f"{name}.l1"),
        l2_subcategory=_normalize_category(raw_rule.get("l2"), field_name=f"{name}.l2"),
        text_patterns=_compile_patterns(raw_rule.get("text_patterns"), field_name=f"{name}.text_patterns"),
        job_name_patterns=_compile_patterns(
            raw_rule.get("job_name_patterns"),
            field_name=f"{name}.job_name_patterns",
        ),
        url_patterns=_compile_patterns(raw_rule.get("url_patterns"), field_name=f"{name}.url_patterns"),
        build_field_patterns=_compile_build_field_patterns(
            raw_rule.get("build_field_patterns"),
            field_name=f"{name}.build_field_patterns",
        ),
    )


def _compile_patterns(raw_patterns: Any, *, field_name: str) -> tuple[re.Pattern[str], ...]:
    if raw_patterns is None:
        return ()
    if not isinstance(raw_patterns, list):
        raise ValueError(f"{field_name} must be a list of regex strings")
    compiled: list[re.Pattern[str]] = []
    for raw_pattern in raw_patterns:
        pattern = str(raw_pattern or "").strip()
        if not pattern:
            continue
        compiled.append(re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE))
    return tuple(compiled)


def _matches_any(patterns: tuple[re.Pattern[str], ...], value: str) -> bool:
    if not patterns:
        return True
    return any(pattern.search(value) for pattern in patterns)


def _compile_build_field_patterns(
    raw_patterns: Any,
    *,
    field_name: str,
) -> Mapping[str, tuple[re.Pattern[str], ...]]:
    if raw_patterns is None:
        return {}
    if not isinstance(raw_patterns, Mapping):
        raise ValueError(f"{field_name} must be an object of field names to regex string lists")
    compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
    for raw_field, raw_field_patterns in raw_patterns.items():
        field = str(raw_field or "").strip()
        if not field:
            raise ValueError(f"{field_name} contains an empty field name")
        compiled[field] = _compile_patterns(raw_field_patterns, field_name=f"{field_name}.{field}")
    return compiled


def _matches_build_fields(
    field_patterns: Mapping[str, tuple[re.Pattern[str], ...]],
    build: Mapping[str, Any],
) -> bool:
    for field, patterns in field_patterns.items():
        if not _matches_any(patterns, str(build.get(field) or "")):
            return False
    return True


def _normalize_category(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized
