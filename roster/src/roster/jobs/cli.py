from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from roster.common.config import get_settings
from roster.common.db import build_engine
from roster.common.logging import configure_logging
from roster.jobs.sync_roster import run_sync_roster
from roster.jobs.validate_lark import validate_lark_roster
from roster.sources.lark import LarkApiClient, LarkRosterSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Roster job runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync-roster", help="Sync Lark roster data into roster tables")
    subparsers.add_parser("validate-lark", help="Fetch Lark roster data and print field quality summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings(require_database=args.command == "sync-roster")
    configure_logging(settings.log_level)

    if args.command == "sync-roster":
        engine = build_engine(settings)
        try:
            source = _build_lark_source(settings) if settings.lark.is_configured else None
            summary = run_sync_roster(engine, source=source)
            logging.getLogger(__name__).info(
                "sync-roster finished",
                extra={"summary": summary.__dict__},
            )
            return 0
        finally:
            engine.dispose()

    if args.command == "validate-lark":
        if not settings.lark.is_configured:
            raise SystemExit("validate-lark requires ROSTER_LARK_APP_ID and ROSTER_LARK_APP_SECRET")
        summary = validate_lark_roster(_build_lark_source(settings))
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")  # pragma: no cover


def _build_lark_source(settings) -> LarkRosterSource:
    return LarkRosterSource(
        LarkApiClient(settings.lark.app_id, settings.lark.app_secret),
        github_custom_attr_id=settings.lark.github_custom_attr_id,
        root_department_id=settings.lark.root_department_id,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
