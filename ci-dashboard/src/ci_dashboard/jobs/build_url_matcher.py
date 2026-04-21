from __future__ import annotations

import re

URL_PREFIXES = (
    "https://do.pingcap.net",
    "https://prow.tidb.net",
)

IDC_HOST = "https://do.pingcap.net"
GCP_HOST = "https://prow.tidb.net"
PROW_NATIVE_PREFIX = "https://prow.tidb.net/view/gs/"
JENKINS_PREFIXES = (
    "https://prow.tidb.net/jenkins/",
    "https://do.pingcap.net/",
)
TRAILING_BUILD_NUMBER_RE = re.compile(r"/\d+$")


def normalize_build_url(url: str | None) -> str | None:
    if url is None:
        return None
    normalized = url.strip()
    if normalized == "":
        return None
    for prefix in URL_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    if normalized.endswith("/display/redirect"):
        normalized = normalized[: -len("/display/redirect")]
    normalized = normalized.rstrip("/")
    return normalized or "/"


def normalized_job_path_from_key(normalized_build_key: str | None) -> str | None:
    if normalized_build_key is None:
        return None
    normalized = normalized_build_key.strip().rstrip("/")
    if normalized == "":
        return None
    normalized = TRAILING_BUILD_NUMBER_RE.sub("", normalized)
    return normalized or "/"


def classify_cloud_phase(url: str | None) -> str:
    if url and url.startswith(GCP_HOST):
        return "GCP"
    return "IDC"


def classify_build_system(url: str | None) -> str:
    if not url:
        return "UNKNOWN"
    if url.startswith(PROW_NATIVE_PREFIX):
        return "PROW_NATIVE"
    if any(url.startswith(prefix) for prefix in JENKINS_PREFIXES):
        return "JENKINS"
    return "UNKNOWN"


def build_job_url(normalized_job_path: str | None, cloud_phase: str | None) -> str | None:
    if normalized_job_path is None:
        return None
    path = normalized_job_path.strip()
    if path == "":
        return None
    if not path.startswith("/"):
        path = f"/{path}"
    host = GCP_HOST if str(cloud_phase or "").upper() == "GCP" else IDC_HOST
    return f"{host}{path}"
