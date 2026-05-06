from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import AnalyzeErrorsSummary, ReviewErrorSummary
from ci_dashboard.jobs.gcs_client import GCSReader, parse_gcs_uri
from ci_dashboard.jobs.llm_classifier import LLMClassifier, build_llm_classifier
from ci_dashboard.jobs.rule_engine import RuleEngine

LOG = logging.getLogger(__name__)

FAILURE_LIKE_STATES = ("failure", "error", "timeout", "timed_out", "aborted")

FETCH_CANDIDATE_BUILDS = text(
    """
    SELECT b.id, b.source_prow_job_id, b.job_name, b.job_type, b.repo_full_name,
           b.pr_number, b.head_sha, b.url, b.log_gcs_uri,
           b.error_l1_category, b.error_l2_subcategory,
           b.revise_error_l1_category, b.revise_error_l2_subcategory,
           pj.state AS prow_state, pj.status AS prow_status,
           CASE
             WHEN EXISTS (
               SELECT 1
               FROM ci_l1_builds newer
               WHERE newer.repo_full_name = b.repo_full_name
                 AND newer.pr_number = b.pr_number
                 AND newer.job_name = b.job_name
                 AND newer.start_time > b.start_time
                 AND newer.id <> b.id
             ) THEN 1 ELSE 0
           END AS has_newer_pr_job_version,
           CASE
             WHEN EXISTS (
               SELECT 1
               FROM ci_l1_builds newer
               WHERE newer.repo_full_name = b.repo_full_name
                 AND newer.pr_number = b.pr_number
                 AND newer.job_name = b.job_name
                 AND newer.start_time > b.start_time
                 AND newer.id <> b.id
                 AND COALESCE(newer.head_sha, '') <> COALESCE(b.head_sha, '')
             ) THEN 1 ELSE 0
           END AS has_newer_pr_job_version_with_different_sha
    FROM ci_l1_builds b
    LEFT JOIN prow_jobs pj ON pj.prowJobId = b.source_prow_job_id
    WHERE (b.log_gcs_uri IS NOT NULL OR (
        pj.state = 'aborted'
        AND EXISTS (
          SELECT 1
          FROM ci_l1_builds newer
          WHERE newer.repo_full_name = b.repo_full_name
            AND newer.pr_number = b.pr_number
            AND newer.job_name = b.job_name
            AND newer.start_time > b.start_time
            AND newer.id <> b.id
        )
      ))
      AND b.state IN :failure_states
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND (:build_id IS NULL OR b.id = :build_id)
      AND (:force = 1 OR b.error_l1_category IS NULL OR b.error_l2_subcategory IS NULL)
    ORDER BY b.start_time DESC, b.id DESC
    LIMIT :limit_value
    """
).bindparams(bindparam("failure_states", expanding=True))

UPDATE_MACHINE_CLASSIFICATION = text(
    """
    UPDATE ci_l1_builds
    SET error_l1_category = :error_l1_category,
        error_l2_subcategory = :error_l2_subcategory,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
      AND revise_error_l1_category IS NULL
      AND revise_error_l2_subcategory IS NULL
    """
)

UPDATE_REVIEW_CLASSIFICATION = text(
    """
    UPDATE ci_l1_builds
    SET revise_error_l1_category = :revise_error_l1_category,
        revise_error_l2_subcategory = :revise_error_l2_subcategory,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """
)


def run_analyze_errors(
    engine: Engine,
    settings: Settings,
    *,
    limit: int | None = None,
    build_id: int | None = None,
    force: bool = False,
    reader: GCSReader | Any | None = None,
    rule_engine: RuleEngine | None = None,
    llm_classifier: LLMClassifier | None = None,
) -> AnalyzeErrorsSummary:
    summary = AnalyzeErrorsSummary()
    resolved_limit = limit or settings.archive.build_limit

    with engine.begin() as connection:
        candidates = list(
            connection.execute(
                FETCH_CANDIDATE_BUILDS,
                {
                    "failure_states": FAILURE_LIKE_STATES,
                    "build_id": build_id,
                    "force": 1 if force else 0,
                    "limit_value": resolved_limit,
                },
            ).mappings()
        )

    if not candidates:
        return summary

    resolved_rule_engine = rule_engine or RuleEngine.from_file()
    resolved_classifier = llm_classifier or build_llm_classifier(
        settings.llm,
        default_l1_category=resolved_rule_engine.default_classification.l1_category,
        default_l2_subcategory=resolved_rule_engine.default_classification.l2_subcategory,
        allowed_classifications=resolved_rule_engine.allowed_classifications,
    )
    resolved_reader = reader or GCSReader()

    for build in candidates:
        summary.builds_scanned += 1
        enriched_build = _enrich_build_evidence(build)
        try:
            classified = _classify_single_build(
                engine,
                enriched_build,
                reader=resolved_reader,
                rule_engine=resolved_rule_engine,
                llm_classifier=resolved_classifier,
            )
        except Exception:
            summary.builds_failed += 1
            LOG.exception("failed to analyze archived Jenkins error log", extra={"build_id": build["id"]})
            continue

        if classified == "rule":
            summary.builds_classified += 1
            summary.builds_rule_classified += 1
        elif classified == "llm":
            summary.builds_classified += 1
            summary.builds_llm_classified += 1
        else:
            summary.builds_skipped += 1

    return summary


def review_error_classification(
    engine: Engine,
    *,
    build_id: int,
    l1_category: str,
    l2_subcategory: str,
) -> ReviewErrorSummary:
    normalized_l1 = _normalize_review_category(l1_category)
    normalized_l2 = _normalize_review_category(l2_subcategory)

    with engine.begin() as connection:
        result = connection.execute(
            UPDATE_REVIEW_CLASSIFICATION,
            {
                "id": build_id,
                "revise_error_l1_category": normalized_l1,
                "revise_error_l2_subcategory": normalized_l2,
            },
        )
    return ReviewErrorSummary(rows_updated=result.rowcount or 0)


def _classify_single_build(
    engine: Engine,
    build: Mapping[str, Any],
    *,
    reader: Any,
    rule_engine: RuleEngine,
    llm_classifier: LLMClassifier,
) -> str:
    classification = rule_engine.classify(log_text="", build=build)
    classification_source = "rule"
    if classification is not None:
        with engine.begin() as connection:
            connection.execute(
                UPDATE_MACHINE_CLASSIFICATION,
                {
                    "id": int(build["id"]),
                    "error_l1_category": classification.l1_category,
                    "error_l2_subcategory": classification.l2_subcategory,
                },
            )
        return classification_source

    object_ref = parse_gcs_uri(build.get("log_gcs_uri"))
    if object_ref is None:
        return "skipped"

    log_text = reader.download_text(bucket=object_ref.bucket, object_name=object_ref.object_name)
    classification = rule_engine.classify(log_text=log_text, build=build)
    if classification is None:
        classification = llm_classifier.classify(log_text=log_text, build=build)
        classification_source = "llm"

    with engine.begin() as connection:
        connection.execute(
            UPDATE_MACHINE_CLASSIFICATION,
            {
                "id": int(build["id"]),
                "error_l1_category": classification.l1_category,
                "error_l2_subcategory": classification.l2_subcategory,
            },
        )
    return classification_source


def _enrich_build_evidence(build: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(build)
    status = _parse_json_mapping(enriched.get("prow_status"))
    description = status.get("description") or status.get("Description")
    if description is not None:
        enriched["prow_status_description"] = str(description)
    for field in (
        "has_newer_pr_job_version",
        "has_newer_pr_job_version_with_different_sha",
    ):
        newer_version = enriched.get(field)
        if isinstance(newer_version, bool):
            enriched[field] = "1" if newer_version else "0"
        elif newer_version is not None:
            enriched[field] = str(newer_version)
    return enriched


def _parse_json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _normalize_review_category(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("review category values must be non-empty")
    return normalized
