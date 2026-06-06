from __future__ import annotations


def source_job_name(base_job_name: str, *, vendor: str, account_id: str) -> str:
    return f"{base_job_name}:{vendor}:{account_id}"
