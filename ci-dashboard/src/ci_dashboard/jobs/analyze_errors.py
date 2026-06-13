from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from ci_dashboard.common.config import Settings
from ci_dashboard.common.models import AnalyzeErrorsSummary, ErrorClassification, ReviewErrorSummary
from ci_dashboard.jobs.gcs_client import GCSReader, parse_gcs_uri
from ci_dashboard.jobs.jenkins_client import JenkinsClient
from ci_dashboard.jobs.llm_classifier import LLMClassifier, build_llm_classifier
from ci_dashboard.jobs.rule_engine import RuleEngine

LOG = logging.getLogger(__name__)

FAILURE_LIKE_STATES = ("failure", "error", "timeout", "timed_out", "aborted")
REMOTING_RULE_SOURCES = {
    "rule:infra_jenkins_agent_offline",
    "rule:infra_jenkins_remoting",
}

FETCH_UNCLASSIFIED_CANDIDATE_IDS = text(
    """
    SELECT b.id, b.start_time
    FROM ci_l1_builds b
    WHERE b.state = :failure_state
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND b.log_gcs_uri IS NOT NULL
      AND (
        b.error_l1_category IS NULL
        OR b.error_l2_subcategory IS NULL
      )
    ORDER BY b.start_time DESC, b.id DESC
    LIMIT :limit_value
    """
)

FETCH_UNCLASSIFIED_CANDIDATE_IDS_FORCE = text(
    """
    SELECT b.id, b.start_time
    FROM ci_l1_builds b
    WHERE b.state = :failure_state
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND b.log_gcs_uri IS NOT NULL
    ORDER BY b.start_time DESC, b.id DESC
    LIMIT :limit_value
    """
)

FETCH_SUPERSEDED_CANDIDATE_IDS = text(
    """
    SELECT b.id, b.start_time
    FROM prow_jobs pj
    JOIN ci_l1_builds b ON b.source_prow_job_id = pj.prowJobId
    WHERE pj.state = 'aborted'
      AND b.state IN :failure_states
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND (
        COALESCE(b.error_l1_category, '') <> 'OTHERS'
        OR COALESCE(b.error_l2_subcategory, '') <> 'SUPERSEDED_BY_NEWER_BUILD'
      )
      AND EXISTS (
        SELECT 1
        FROM ci_l1_builds newer
        WHERE newer.repo_full_name = b.repo_full_name
          AND newer.pr_number = b.pr_number
          AND newer.job_name = b.job_name
          AND newer.start_time > b.start_time
          AND newer.id <> b.id
      )
    ORDER BY b.start_time DESC, b.id DESC
    LIMIT :limit_value
    """
).bindparams(bindparam("failure_states", expanding=True))

FETCH_SUPERSEDED_CANDIDATE_IDS_FORCE = text(
    """
    SELECT b.id, b.start_time
    FROM prow_jobs pj
    JOIN ci_l1_builds b ON b.source_prow_job_id = pj.prowJobId
    WHERE pj.state = 'aborted'
      AND b.state IN :failure_states
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND EXISTS (
        SELECT 1
        FROM ci_l1_builds newer
        WHERE newer.repo_full_name = b.repo_full_name
          AND newer.pr_number = b.pr_number
          AND newer.job_name = b.job_name
          AND newer.start_time > b.start_time
          AND newer.id <> b.id
      )
    ORDER BY b.start_time DESC, b.id DESC
    LIMIT :limit_value
    """
).bindparams(bindparam("failure_states", expanding=True))

FETCH_CANDIDATE_BUILDS_BY_IDS = text(
    """
    SELECT b.id, b.source_prow_job_id, b.job_name, b.job_type, b.repo_full_name,
           b.pr_number, b.head_sha, b.normalized_build_url AS url, b.normalized_build_url,
           b.pod_name, b.log_gcs_uri,
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
    WHERE b.id IN :build_ids
    ORDER BY b.start_time DESC, b.id DESC
    """
).bindparams(bindparam("build_ids", expanding=True))

FETCH_CANDIDATE_BUILD_BY_ID = text(
    """
    SELECT b.id, b.source_prow_job_id, b.job_name, b.job_type, b.repo_full_name,
           b.pr_number, b.head_sha, b.normalized_build_url AS url, b.normalized_build_url,
           b.pod_name, b.log_gcs_uri,
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
    WHERE b.id = :build_id
      AND b.state IN :failure_states
      AND b.revise_error_l1_category IS NULL
      AND b.revise_error_l2_subcategory IS NULL
      AND (b.log_gcs_uri IS NOT NULL OR (
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
      AND (
        :force = 1
        OR b.error_l1_category IS NULL
        OR b.error_l2_subcategory IS NULL
        OR (
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
          AND (
            COALESCE(b.error_l1_category, '') <> 'OTHERS'
            OR COALESCE(b.error_l2_subcategory, '') <> 'SUPERSEDED_BY_NEWER_BUILD'
          )
        )
      )
    """
).bindparams(bindparam("failure_states", expanding=True))

FETCH_POD_LIFECYCLE_EVIDENCE_BY_PROW_JOB_ID = text(
    """
    SELECT id, source_prow_job_id, normalized_build_url, pod_name, abnormal_reason, abnormal_message
    FROM ci_l1_pod_lifecycle
    WHERE source_prow_job_id IN :source_prow_job_ids
    ORDER BY id DESC
    """
).bindparams(bindparam("source_prow_job_ids", expanding=True))

FETCH_POD_LIFECYCLE_EVIDENCE_BY_BUILD_URL = text(
    """
    SELECT id, source_prow_job_id, normalized_build_url, pod_name, abnormal_reason, abnormal_message
    FROM ci_l1_pod_lifecycle
    WHERE normalized_build_url IN :normalized_build_urls
    ORDER BY id DESC
    """
).bindparams(bindparam("normalized_build_urls", expanding=True))

FETCH_POD_EVENT_EVIDENCE = text(
    """
    SELECT pod_name, reporting_instance, event_reason, event_type, event_message
    FROM ci_l1_pod_events
    WHERE pod_name IN :pod_names
    ORDER BY event_timestamp DESC, id DESC
    """
).bindparams(bindparam("pod_names", expanding=True))

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
    jenkins_fetcher: JenkinsClient | Any | None = None,
    rule_engine: RuleEngine | None = None,
    llm_classifier: LLMClassifier | None = None,
) -> AnalyzeErrorsSummary:
    summary = AnalyzeErrorsSummary()
    resolved_limit = limit or settings.archive.build_limit
    pending_updates: list[dict[str, Any]] = []

    with engine.begin() as connection:
        if build_id is not None:
            candidates = list(
                connection.execute(
                    FETCH_CANDIDATE_BUILD_BY_ID,
                    {
                        "failure_states": FAILURE_LIKE_STATES,
                        "build_id": build_id,
                        "force": 1 if force else 0,
                    },
                ).mappings()
            )
        else:
            candidates = _fetch_candidate_builds_scan(
                connection,
                limit=resolved_limit,
                force=force,
            )
        pod_evidence_by_build_id = _load_pod_evidence_by_build_id(connection, candidates)

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
    resolved_jenkins_fetcher = jenkins_fetcher or JenkinsClient(settings.jenkins)
    owns_jenkins_fetcher = jenkins_fetcher is None and hasattr(resolved_jenkins_fetcher, "close")

    try:
        for build in candidates:
            summary.builds_scanned += 1
            enriched_build = _enrich_build_evidence(
                build,
                pod_evidence=pod_evidence_by_build_id.get(int(build["id"])),
            )
            if not force and not _should_refresh_existing_machine_classification(enriched_build):
                summary.builds_skipped += 1
                continue
            try:
                classification_source, classification = _classify_single_build(
                    enriched_build,
                    reader=resolved_reader,
                    jenkins_fetcher=resolved_jenkins_fetcher,
                    rule_engine=resolved_rule_engine,
                    llm_classifier=resolved_classifier,
                )
            except Exception:
                summary.builds_failed += 1
                LOG.exception("failed to analyze archived Jenkins error log", extra={"build_id": build["id"]})
                continue

            if classification_source == "rule" and classification is not None:
                pending_updates.append(
                    {
                        "id": int(build["id"]),
                        "error_l1_category": classification.l1_category,
                        "error_l2_subcategory": classification.l2_subcategory,
                    }
                )
                summary.builds_classified += 1
                summary.builds_rule_classified += 1
            elif classification_source == "llm" and classification is not None:
                pending_updates.append(
                    {
                        "id": int(build["id"]),
                        "error_l1_category": classification.l1_category,
                        "error_l2_subcategory": classification.l2_subcategory,
                    }
                )
                summary.builds_classified += 1
                summary.builds_llm_classified += 1
            else:
                summary.builds_skipped += 1
    finally:
        if owns_jenkins_fetcher:
            resolved_jenkins_fetcher.close()

    if pending_updates:
        with engine.begin() as connection:
            connection.execute(UPDATE_MACHINE_CLASSIFICATION, pending_updates)

    return summary


def _fetch_candidate_builds_scan(
    connection: Any,
    *,
    limit: int,
    force: bool,
) -> list[Mapping[str, Any]]:
    candidate_rows: dict[int, Mapping[str, Any]] = {}
    unclassified_query = (
        FETCH_UNCLASSIFIED_CANDIDATE_IDS_FORCE if force else FETCH_UNCLASSIFIED_CANDIDATE_IDS
    )
    superseded_query = (
        FETCH_SUPERSEDED_CANDIDATE_IDS_FORCE if force else FETCH_SUPERSEDED_CANDIDATE_IDS
    )

    for failure_state in FAILURE_LIKE_STATES:
        params = {"failure_state": failure_state, "limit_value": limit}
        for row in connection.execute(unclassified_query, params).mappings():
            candidate_rows[int(row["id"])] = row

    for row in connection.execute(
        superseded_query,
        {"failure_states": FAILURE_LIKE_STATES, "limit_value": limit},
    ).mappings():
        candidate_rows[int(row["id"])] = row

    selected_rows = sorted(
        candidate_rows.values(),
        key=lambda row: (str(row["start_time"] or ""), int(row["id"])),
        reverse=True,
    )[:limit]
    if not selected_rows:
        return []

    return list(
        connection.execute(
            FETCH_CANDIDATE_BUILDS_BY_IDS,
            {"build_ids": [int(row["id"]) for row in selected_rows]},
        ).mappings()
    )


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
    build: Mapping[str, Any],
    *,
    reader: Any,
    jenkins_fetcher: Any,
    rule_engine: RuleEngine,
    llm_classifier: LLMClassifier,
) -> tuple[str, ErrorClassification | None]:
    classification = rule_engine.classify(log_text="", build=build)
    classification_source = "rule"
    if classification is not None:
        return classification_source, classification

    object_ref = parse_gcs_uri(build.get("log_gcs_uri"))
    if object_ref is None:
        return "skipped", None

    log_text = reader.download_text(bucket=object_ref.bucket, object_name=object_ref.object_name)
    classification = rule_engine.classify(log_text=log_text, build=build)
    if classification is not None and classification.source in REMOTING_RULE_SOURCES:
        combined_log = _append_live_signal_excerpts(
            log_text,
            build=build,
            jenkins_fetcher=jenkins_fetcher,
        )
        if combined_log != log_text:
            reclassified = rule_engine.classify(log_text=combined_log, build=build)
            if reclassified is not None:
                classification = reclassified
            log_text = combined_log
    if classification is None:
        classification = llm_classifier.classify(log_text=log_text, build=build)
        classification_source = "llm"

    return classification_source, classification


def _load_pod_evidence_by_build_id(connection, builds: list[Mapping[str, Any]]) -> dict[int, dict[str, str]]:
    source_prow_job_ids = sorted(
        {
            str(build.get("source_prow_job_id") or "").strip()
            for build in builds
            if str(build.get("source_prow_job_id") or "").strip()
        }
    )
    normalized_build_urls = sorted(
        {
            str(build.get("normalized_build_url") or "").strip()
            for build in builds
            if str(build.get("normalized_build_url") or "").strip()
        }
    )
    direct_pod_names = {
        str(build.get("pod_name") or "").strip()
        for build in builds
        if str(build.get("pod_name") or "").strip()
    }
    if not source_prow_job_ids and not normalized_build_urls and not direct_pod_names:
        return {}

    lifecycle_rows: list[Mapping[str, Any]] = []
    if source_prow_job_ids:
        lifecycle_rows.extend(
            connection.execute(
                FETCH_POD_LIFECYCLE_EVIDENCE_BY_PROW_JOB_ID,
                {"source_prow_job_ids": source_prow_job_ids},
            ).mappings()
        )
    if normalized_build_urls:
        lifecycle_rows.extend(
            connection.execute(
                FETCH_POD_LIFECYCLE_EVIDENCE_BY_BUILD_URL,
                {"normalized_build_urls": normalized_build_urls},
            ).mappings()
        )

    lifecycle_by_prow_job_id: dict[str, Mapping[str, Any]] = {}
    lifecycle_by_build_url: dict[str, Mapping[str, Any]] = {}
    pod_names: set[str] = set()
    for row in lifecycle_rows:
        prow_job_id = str(row.get("source_prow_job_id") or "").strip()
        if not prow_job_id or prow_job_id in lifecycle_by_prow_job_id:
            pass
        elif prow_job_id:
            lifecycle_by_prow_job_id[prow_job_id] = row
        normalized_build_url = str(row.get("normalized_build_url") or "").strip()
        if normalized_build_url and normalized_build_url not in lifecycle_by_build_url:
            lifecycle_by_build_url[normalized_build_url] = row
        pod_name = str(row.get("pod_name") or "").strip()
        if pod_name:
            pod_names.add(pod_name)
    pod_names.update(direct_pod_names)

    event_rows = []
    if pod_names:
        event_rows = list(
            connection.execute(
                FETCH_POD_EVENT_EVIDENCE,
                {"pod_names": sorted(pod_names)},
            ).mappings()
        )

    event_summary_by_pod_name: dict[str, dict[str, str]] = {}
    for row in event_rows:
        pod_name = str(row.get("pod_name") or "").strip()
        if not pod_name:
            continue
        summary = event_summary_by_pod_name.setdefault(
            pod_name,
            {
                "pod_event_reporting_instances": "",
                "pod_event_reasons": "",
                "pod_event_types": "",
                "pod_event_messages": "",
            },
        )
        _append_newline_separated_token(summary, "pod_event_reporting_instances", row.get("reporting_instance"))
        _append_newline_separated_token(summary, "pod_event_reasons", row.get("event_reason"))
        _append_newline_separated_token(summary, "pod_event_types", row.get("event_type"))
        _append_newline_separated_token(summary, "pod_event_messages", row.get("event_message"))

    evidence_by_build_id: dict[int, dict[str, str]] = {}
    for build in builds:
        build_id = int(build["id"])
        prow_job_id = str(build.get("source_prow_job_id") or "").strip()
        normalized_build_url = str(build.get("normalized_build_url") or "").strip()
        lifecycle = lifecycle_by_prow_job_id.get(prow_job_id)
        if lifecycle is None and normalized_build_url:
            lifecycle = lifecycle_by_build_url.get(normalized_build_url)
        evidence: dict[str, str] = {}
        pod_name = ""
        if lifecycle is not None:
            pod_name = str(lifecycle.get("pod_name") or "").strip()
        if not pod_name:
            pod_name = str(build.get("pod_name") or "").strip()
        if pod_name:
            evidence["pod_lifecycle_pod_name"] = pod_name
        if lifecycle is not None:
            abnormal_reason = str(lifecycle.get("abnormal_reason") or "").strip()
            if abnormal_reason:
                evidence["pod_lifecycle_abnormal_reason"] = abnormal_reason
            abnormal_message = str(lifecycle.get("abnormal_message") or "").strip()
            if abnormal_message:
                evidence["pod_lifecycle_abnormal_message"] = abnormal_message
        if pod_name and pod_name in event_summary_by_pod_name:
            evidence.update(event_summary_by_pod_name[pod_name])
        if evidence:
            evidence_by_build_id[build_id] = evidence
    return evidence_by_build_id


def _append_newline_separated_token(summary: dict[str, str], key: str, raw_value: Any) -> None:
    value = str(raw_value or "").strip()
    if not value:
        return
    existing = summary.get(key, "")
    if not existing:
        summary[key] = value
        return
    existing_items = existing.split("\n")
    if value not in existing_items:
        existing_items.append(value)
        summary[key] = "\n".join(existing_items)


def _enrich_build_evidence(build: Mapping[str, Any], *, pod_evidence: Mapping[str, str] | None = None) -> dict[str, Any]:
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
    if pod_evidence:
        enriched.update(pod_evidence)
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


def _should_refresh_existing_machine_classification(build: Mapping[str, Any]) -> bool:
    error_l1 = str(build.get("error_l1_category") or "").strip().upper()
    error_l2 = str(build.get("error_l2_subcategory") or "").strip().upper()
    if not error_l1 or not error_l2:
        return True
    if error_l1 == "OTHERS" and error_l2 == "SUPERSEDED_BY_NEWER_BUILD":
        return False

    prow_state = str(build.get("prow_state") or "").strip().lower()
    if prow_state != "aborted":
        return False

    if str(build.get("has_newer_pr_job_version_with_different_sha") or "").strip().lower() in {"1", "true"}:
        return True

    status_description = str(build.get("prow_status_description") or "").strip()
    if (
        status_description == "Aborted as the newer version of this job is running."
        and str(build.get("has_newer_pr_job_version") or "").strip().lower() in {"1", "true"}
    ):
        return True

    return False


def _normalize_review_category(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("review category values must be non-empty")
    return normalized


def _append_live_signal_excerpts(
    log_text: str,
    *,
    build: Mapping[str, Any],
    jenkins_fetcher: Any,
) -> str:
    fetch_console_signal_excerpts = getattr(jenkins_fetcher, "fetch_console_signal_excerpts", None)
    if not callable(fetch_console_signal_excerpts):
        return log_text

    build_url = str(build.get("url") or "").strip()
    if not build_url:
        return log_text

    signal_excerpts = fetch_console_signal_excerpts(build_url)
    if not str(signal_excerpts or "").strip():
        return log_text
    return f"{log_text.rstrip()}\n{signal_excerpts}"
