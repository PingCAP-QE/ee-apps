from __future__ import annotations

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
    SELECT id, job_name, url, log_gcs_uri, error_l1_category, error_l2_subcategory,
           revise_error_l1_category, revise_error_l2_subcategory
    FROM ci_l1_builds
    WHERE log_gcs_uri IS NOT NULL
      AND state IN :failure_states
      AND revise_error_l1_category IS NULL
      AND revise_error_l2_subcategory IS NULL
      AND (:build_id IS NULL OR id = :build_id)
      AND (:force = 1 OR error_l1_category IS NULL OR error_l2_subcategory IS NULL)
    ORDER BY start_time DESC, id DESC
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
        try:
            classified = _classify_single_build(
                engine,
                build,
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
    object_ref = parse_gcs_uri(build.get("log_gcs_uri"))
    if object_ref is None:
        return "skipped"

    log_text = reader.download_text(bucket=object_ref.bucket, object_name=object_ref.object_name)
    classification = rule_engine.classify(log_text=log_text, build=build)
    classification_source = "rule"
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


def _normalize_review_category(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("review category values must be non-empty")
    return normalized
