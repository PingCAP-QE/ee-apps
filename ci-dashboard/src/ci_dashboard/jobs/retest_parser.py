from __future__ import annotations

SUPPORTED_RETEST_COMMANDS = {"/retest", "/retest-required"}


def normalize_command(body: str) -> str:
    return " ".join(body.strip().split())


def is_supported_retest_command(body: str | None) -> bool:
    if body is None:
        return False
    return normalize_command(body) in SUPPORTED_RETEST_COMMANDS
