from __future__ import annotations

from sqlalchemy import create_engine, text

from roster.jobs.sync_roster import FetchedEmployee, FetchedRoster, StaticRosterSource, metadata, run_sync_roster
from roster.jobs.validate_history import validate_historical_employees


def test_validate_historical_employees_compares_email_matched_github_ids() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    _create_historical_tables(engine)
    run_sync_roster(
        engine,
        source=StaticRosterSource(
            FetchedRoster(
                employees=[
                    FetchedEmployee(
                        lark_id="alice",
                        name="Alice Zhang",
                        email="alice@example.com",
                        github_id="alice-gh",
                    ),
                    FetchedEmployee(
                        lark_id="bob",
                        name="Bob Li",
                        email="bob@example.com",
                        github_id="bob-roster",
                    ),
                    FetchedEmployee(
                        lark_id="carol",
                        name="Carol Wu",
                        email="carol@example.com",
                    ),
                    FetchedEmployee(
                        lark_id="dave",
                        name="Dave Xu",
                        email="dave@example.com",
                        github_id="dave-gh",
                    ),
                ]
            )
        ),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO employee_details (name, email, dept, `sub-dept`, active, githubid)
                VALUES
                  ('Alice Legacy', 'alice@example.com', 'eng', 'db', 1, 'alice-gh'),
                  ('Bob Legacy', 'bob@example.com', 'eng', 'db', 1, 'bob-legacy'),
                  ('Carol Legacy', 'carol@example.com', 'eng', 'db', 1, 'carol-gh'),
                  ('Eve Legacy', 'eve@example.com', 'eng', 'db', 1, 'eve-gh')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO `pingcap-ids`
                  (email, name, displayname, githubid, githubname, githubemail)
                VALUES
                  ('alice@example.com', 'Alice', 'Alice Zhang', 'alice-gh', 'AliceGH', ''),
                  ('bob@example.com', 'Bob', 'Bob Li', 'bob-legacy', 'BobGH', 'bob@github.test'),
                  ('dave@example.com', 'Dave', 'Dave Xu', '', 'DaveGH', '')
                """
            )
        )

    report = validate_historical_employees(engine, details_limit=10)
    summaries = {summary.source: summary for summary in report.summaries}

    assert summaries["employee_details"].legacy_rows == 4
    assert summaries["employee_details"].matched_emails == 3
    assert summaries["employee_details"].legacy_rows_missing_from_roster == 1
    assert summaries["employee_details"].github_matches == 1
    assert summaries["employee_details"].github_mismatches == 1
    assert summaries["employee_details"].roster_missing_github_with_legacy_github == 1

    assert summaries["pingcap-ids"].legacy_rows == 3
    assert summaries["pingcap-ids"].matched_emails == 3
    assert summaries["pingcap-ids"].github_matches == 1
    assert summaries["pingcap-ids"].github_mismatches == 1
    assert summaries["pingcap-ids"].legacy_missing_github_with_roster_github == 1

    mismatch = next(item for item in report.github_mismatches if item.source == "pingcap-ids")
    assert mismatch.email == "bob@example.com"
    assert mismatch.roster_github_id == "bob-roster"
    assert mismatch.legacy_github_id == "bob-legacy"
    assert mismatch.legacy_display_name == "Bob Li"
    assert mismatch.legacy_github_email == "bob@github.test"


def test_validate_historical_employees_can_omit_details() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata.create_all(engine)
    _create_historical_tables(engine)

    report = validate_historical_employees(engine, details_limit=0)

    assert len(report.summaries) == 2
    assert report.github_mismatches == ()


def _create_historical_tables(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE employee_details (
                  name TEXT,
                  email TEXT NOT NULL,
                  dept TEXT,
                  `sub-dept` TEXT,
                  active INTEGER,
                  githubid TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE `pingcap-ids` (
                  email TEXT,
                  name TEXT,
                  displayname TEXT,
                  githubid TEXT,
                  githubname TEXT,
                  githubemail TEXT
                )
                """
            )
        )
