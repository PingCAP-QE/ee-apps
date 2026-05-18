from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class HistoricalEmployeeSource:
    name: str
    table_sql: str
    email_sql: str
    github_sql: str
    name_sql: str
    display_name_sql: str = "NULL"
    github_name_sql: str = "NULL"
    github_email_sql: str = "NULL"


DEFAULT_HISTORICAL_EMPLOYEE_SOURCES = (
    HistoricalEmployeeSource(
        name="employee_details",
        table_sql="employee_details",
        email_sql="email",
        github_sql="githubid",
        name_sql="name",
    ),
    HistoricalEmployeeSource(
        name="pingcap-ids",
        table_sql="`pingcap-ids`",
        email_sql="email",
        github_sql="githubid",
        name_sql="name",
        display_name_sql="displayname",
        github_name_sql="githubname",
        github_email_sql="githubemail",
    ),
)


@dataclass(frozen=True)
class HistoricalEmployeeValidationSummary:
    source: str
    legacy_rows: int
    legacy_rows_with_email: int
    legacy_distinct_emails: int
    legacy_duplicate_emails: int
    roster_rows_with_email: int
    matched_rows: int
    matched_emails: int
    legacy_rows_missing_from_roster: int
    roster_emails_missing_from_legacy: int
    github_matches: int
    github_mismatches: int
    roster_missing_github_with_legacy_github: int
    legacy_missing_github_with_roster_github: int
    both_missing_github: int


@dataclass(frozen=True)
class HistoricalEmployeeGithubMismatch:
    source: str
    email: str
    roster_name: str | None
    legacy_name: str | None
    legacy_display_name: str | None
    roster_github_id: str
    legacy_github_id: str
    legacy_github_name: str | None
    legacy_github_email: str | None


@dataclass(frozen=True)
class HistoricalEmployeeValidationReport:
    summaries: tuple[HistoricalEmployeeValidationSummary, ...]
    github_mismatches: tuple[HistoricalEmployeeGithubMismatch, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summaries": [asdict(summary) for summary in self.summaries],
            "github_mismatches": [asdict(mismatch) for mismatch in self.github_mismatches],
        }


def validate_historical_employees(
    engine: Engine,
    *,
    sources: tuple[HistoricalEmployeeSource, ...] = DEFAULT_HISTORICAL_EMPLOYEE_SOURCES,
    details_limit: int = 20,
) -> HistoricalEmployeeValidationReport:
    summaries: list[HistoricalEmployeeValidationSummary] = []
    github_mismatches: list[HistoricalEmployeeGithubMismatch] = []
    with engine.connect() as connection:
        for source in sources:
            summary_row = connection.execute(text(_summary_sql(source))).one()._mapping
            summaries.append(
                HistoricalEmployeeValidationSummary(
                    source=source.name,
                    **{key: _int_value(summary_row[key]) for key in summary_row if key != "source"},
                )
            )
            if details_limit > 0:
                rows = connection.execute(
                    text(_github_mismatch_sql(source)),
                    {"details_limit": details_limit},
                ).all()
                github_mismatches.extend(
                    HistoricalEmployeeGithubMismatch(
                        source=source.name,
                        email=_optional_str(row.email) or "",
                        roster_name=_optional_str(row.roster_name),
                        legacy_name=_optional_str(row.legacy_name),
                        legacy_display_name=_optional_str(row.legacy_display_name),
                        roster_github_id=_optional_str(row.roster_github_id) or "",
                        legacy_github_id=_optional_str(row.legacy_github_id) or "",
                        legacy_github_name=_optional_str(row.legacy_github_name),
                        legacy_github_email=_optional_str(row.legacy_github_email),
                    )
                    for row in rows
                )
    return HistoricalEmployeeValidationReport(
        summaries=tuple(summaries),
        github_mismatches=tuple(github_mismatches),
    )


def _summary_sql(source: HistoricalEmployeeSource) -> str:
    return f"""
WITH
legacy AS (
  SELECT
    NULLIF(LOWER(TRIM({source.email_sql})), '') AS email_key,
    NULLIF(TRIM({source.github_sql}), '') AS github_id
  FROM {source.table_sql}
),
roster AS (
  SELECT
    NULLIF(LOWER(TRIM(email)), '') AS email_key,
    NULLIF(TRIM(github_id), '') AS github_id
  FROM roster_employees
),
matched AS (
  SELECT
    l.email_key,
    l.github_id AS legacy_github_id,
    r.github_id AS roster_github_id
  FROM legacy l
  JOIN roster r ON r.email_key = l.email_key
  WHERE l.email_key IS NOT NULL
),
legacy_duplicate_emails AS (
  SELECT email_key
  FROM legacy
  WHERE email_key IS NOT NULL
  GROUP BY email_key
  HAVING COUNT(*) > 1
)
SELECT
  COUNT(*) AS legacy_rows,
  SUM(CASE WHEN l.email_key IS NOT NULL THEN 1 ELSE 0 END) AS legacy_rows_with_email,
  COUNT(DISTINCT l.email_key) AS legacy_distinct_emails,
  (SELECT COUNT(*) FROM legacy_duplicate_emails) AS legacy_duplicate_emails,
  (SELECT COUNT(*) FROM roster WHERE email_key IS NOT NULL) AS roster_rows_with_email,
  (SELECT COUNT(*) FROM matched) AS matched_rows,
  (SELECT COUNT(DISTINCT email_key) FROM matched) AS matched_emails,
  SUM(
    CASE
      WHEN l.email_key IS NOT NULL AND r.email_key IS NULL THEN 1
      ELSE 0
    END
  ) AS legacy_rows_missing_from_roster,
  (
    SELECT COUNT(DISTINCT r2.email_key)
    FROM roster r2
    LEFT JOIN legacy l2 ON l2.email_key = r2.email_key
    WHERE r2.email_key IS NOT NULL AND l2.email_key IS NULL
  ) AS roster_emails_missing_from_legacy,
  (
    SELECT SUM(
      CASE
        WHEN legacy_github_id IS NOT NULL
          AND roster_github_id IS NOT NULL
          AND LOWER(legacy_github_id) = LOWER(roster_github_id)
        THEN 1
        ELSE 0
      END
    )
    FROM matched
  ) AS github_matches,
  (
    SELECT SUM(
      CASE
        WHEN legacy_github_id IS NOT NULL
          AND roster_github_id IS NOT NULL
          AND LOWER(legacy_github_id) <> LOWER(roster_github_id)
        THEN 1
        ELSE 0
      END
    )
    FROM matched
  ) AS github_mismatches,
  (
    SELECT SUM(
      CASE
        WHEN legacy_github_id IS NOT NULL AND roster_github_id IS NULL THEN 1
        ELSE 0
      END
    )
    FROM matched
  ) AS roster_missing_github_with_legacy_github,
  (
    SELECT SUM(
      CASE
        WHEN legacy_github_id IS NULL AND roster_github_id IS NOT NULL THEN 1
        ELSE 0
      END
    )
    FROM matched
  ) AS legacy_missing_github_with_roster_github,
  (
    SELECT SUM(
      CASE
        WHEN legacy_github_id IS NULL AND roster_github_id IS NULL THEN 1
        ELSE 0
      END
    )
    FROM matched
  ) AS both_missing_github
FROM legacy l
LEFT JOIN roster r ON r.email_key = l.email_key
"""


def _github_mismatch_sql(source: HistoricalEmployeeSource) -> str:
    return f"""
WITH
legacy AS (
  SELECT
    NULLIF(LOWER(TRIM({source.email_sql})), '') AS email_key,
    NULLIF(TRIM({source.email_sql}), '') AS email,
    NULLIF(TRIM({source.github_sql}), '') AS github_id,
    NULLIF(TRIM({source.name_sql}), '') AS name,
    NULLIF(TRIM({source.display_name_sql}), '') AS display_name,
    NULLIF(TRIM({source.github_name_sql}), '') AS github_name,
    NULLIF(TRIM({source.github_email_sql}), '') AS github_email
  FROM {source.table_sql}
),
roster AS (
  SELECT
    NULLIF(LOWER(TRIM(email)), '') AS email_key,
    NULLIF(TRIM(name), '') AS name,
    NULLIF(TRIM(github_id), '') AS github_id
  FROM roster_employees
)
SELECT
  l.email,
  r.name AS roster_name,
  l.name AS legacy_name,
  l.display_name AS legacy_display_name,
  r.github_id AS roster_github_id,
  l.github_id AS legacy_github_id,
  l.github_name AS legacy_github_name,
  l.github_email AS legacy_github_email
FROM legacy l
JOIN roster r ON r.email_key = l.email_key
WHERE l.github_id IS NOT NULL
  AND r.github_id IS NOT NULL
  AND LOWER(l.github_id) <> LOWER(r.github_id)
ORDER BY l.email
LIMIT :details_limit
"""


def _int_value(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
