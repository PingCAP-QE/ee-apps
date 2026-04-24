from __future__ import annotations

import re
from urllib import parse as urllib_parse

GCP_HOST = "https://prow.tidb.net"
PROW_NATIVE_PREFIX = "https://prow.tidb.net/view/gs/"
JENKINS_PREFIXES = (
    "https://prow.tidb.net/jenkins/",
    "https://do.pingcap.net/",
)
INTERNAL_JENKINS_HOST_PREFIXES = (
    "http://jenkins.jenkins.svc.cluster.local",
    "https://jenkins.jenkins.svc.cluster.local",
)
CANONICAL_JENKINS_PATH_PREFIX = "/jenkins/job/"
CANONICAL_PROW_PATH_PREFIX = "/view/gs/"
TRAILING_BUILD_NUMBER_RE = re.compile(r"/\d+/?$")


def normalize_build_url(url: str | None) -> str | None:
    if url is None:
        return None
    normalized = url.strip()
    if normalized == "":
        return None
    if normalized.startswith(("http://", "https://")):
        parsed = urllib_parse.urlparse(normalized)
        path = parsed.path or ""
    else:
        path = normalized

    path = path.strip()
    if path == "":
        return None
    if path.endswith("/display/redirect"):
        path = path[: -len("/display/redirect")]
    if not path.startswith("/"):
        path = f"/{path.lstrip('/')}"
    if path.startswith("/job/"):
        path = f"/jenkins{path}"
    if path.startswith(CANONICAL_JENKINS_PATH_PREFIX) or path.startswith(CANONICAL_PROW_PATH_PREFIX):
        return _canonical_full_url(path)

    if any(normalized.startswith(prefix) for prefix in INTERNAL_JENKINS_HOST_PREFIXES) and path.startswith(
        CANONICAL_JENKINS_PATH_PREFIX
    ):
        return _canonical_full_url(path)
    return None


def normalized_job_path_from_key(normalized_build_url: str | None) -> str | None:
    normalized = normalize_build_url(normalized_build_url)
    if normalized is None:
        return None
    stripped = normalized.rstrip("/")
    if stripped == "":
        return None
    job_url = TRAILING_BUILD_NUMBER_RE.sub("", stripped).rstrip("/")
    if job_url == "":
        return None
    return f"{job_url}/"


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
    if path.startswith(("http://", "https://")):
        return path if path.endswith("/") else f"{path}/"
    if not path.startswith("/"):
        path = f"/{path}"
    host = GCP_HOST if str(cloud_phase or "").upper() == "GCP" else "https://do.pingcap.net"
    return f"{host}{path}"


def _canonical_full_url(path: str) -> str:
    normalized_path = path.rstrip("/")
    if normalized_path == "":
        return f"{GCP_HOST}/"
    return f"{GCP_HOST}{normalized_path}/"
