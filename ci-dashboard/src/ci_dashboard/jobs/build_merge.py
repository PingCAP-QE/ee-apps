from __future__ import annotations

import logging
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

LOG = logging.getLogger(__name__)


def fetch_existing_build_targets(
    connection: Connection,
    *,
    normalized_build_urls: list[str] | tuple[str, ...],
    source_prow_job_ids: list[str] | tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    if not normalized_build_urls and not source_prow_job_ids:
        return {}, {}

    result = connection.execute(
        _build_existing_targets_query(),
        {
            "normalized_build_urls": list(normalized_build_urls),
            "source_prow_job_ids": list(source_prow_job_ids),
        },
    )

    existing_by_prow_job_id: dict[str, dict[str, Any]] = {}
    existing_by_build_url: dict[str, list[dict[str, Any]]] = {}
    for existing in result.mappings():
        payload = dict(existing)
        source_prow_job_id = payload.get("source_prow_job_id")
        if source_prow_job_id:
            existing_by_prow_job_id[str(source_prow_job_id)] = payload
        normalized_build_url = payload.get("normalized_build_url")
        if normalized_build_url:
            existing_by_build_url.setdefault(str(normalized_build_url), []).append(payload)

    for candidates in existing_by_build_url.values():
        candidates.sort(key=lambda candidate: int(candidate["id"]))

    return existing_by_prow_job_id, existing_by_build_url


def resolve_merge_target_id(
    *,
    normalized_build_url: str | None,
    source_prow_job_id: str | None,
    existing_by_prow_job_id: Mapping[str, Mapping[str, Any]],
    existing_by_build_url: Mapping[str, list[Mapping[str, Any]]],
    log_context: Mapping[str, Any] | None = None,
) -> int | None:
    if source_prow_job_id:
        exact_match = existing_by_prow_job_id.get(source_prow_job_id)
        if exact_match is not None:
            return int(exact_match["id"])

    if not normalized_build_url:
        return None

    candidates = list(existing_by_build_url.get(normalized_build_url, ()))
    if not candidates:
        return None

    if source_prow_job_id is None:
        if len(candidates) == 1:
            return int(candidates[0]["id"])

        unresolved_candidates = [candidate for candidate in candidates if not candidate.get("source_prow_job_id")]
        if len(unresolved_candidates) == 1:
            return int(unresolved_candidates[0]["id"])

        resolved_candidates = [candidate for candidate in candidates if candidate.get("source_prow_job_id")]
        if len(resolved_candidates) == 1:
            return int(resolved_candidates[0]["id"])

    conflicting_source_ids = sorted(
        str(candidate["source_prow_job_id"])
        for candidate in candidates
        if candidate.get("source_prow_job_id") not in {None, source_prow_job_id}
    )
    if conflicting_source_ids:
        raise ValueError(
            "normalized_build_url already belongs to a different source_prow_job_id: "
            f"{normalized_build_url} -> {conflicting_source_ids}"
        )

    unresolved_candidates = [candidate for candidate in candidates if not candidate.get("source_prow_job_id")]
    if len(unresolved_candidates) == 1:
        return int(unresolved_candidates[0]["id"])
    if len(unresolved_candidates) > 1:
        chosen = unresolved_candidates[0]
        extra = {
            "normalized_build_url": normalized_build_url,
            "candidate_ids": [int(candidate["id"]) for candidate in unresolved_candidates],
            "incoming_source_prow_job_id": source_prow_job_id,
            "chosen_id": int(chosen["id"]),
        }
        if log_context:
            extra.update(log_context)
        LOG.warning(
            "multiple canonical build rows share normalized_build_url; merging into the oldest unresolved row",
            extra=extra,
        )
        return int(chosen["id"])

    return None


def _build_existing_targets_query():
    return text(
        """
        SELECT id, normalized_build_url, source_prow_job_id
        FROM ci_l1_builds
        WHERE normalized_build_url IN :normalized_build_urls
           OR source_prow_job_id IN :source_prow_job_ids
        """
    ).bindparams(
        bindparam("normalized_build_urls", expanding=True),
        bindparam("source_prow_job_ids", expanding=True),
    )
