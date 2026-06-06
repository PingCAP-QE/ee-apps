from __future__ import annotations

import re
from urllib import parse as urllib_parse

GCP_HOST = "https://prow.tidb.net"
IDC_HOST = "https://do.pingcap.net"
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
    canonical_host: str | None = None
    if normalized.startswith(("http://", "https://")):
        parsed = urllib_parse.urlparse(normalized)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if host == "prow.tidb.net":
            canonical_host = GCP_HOST
        elif host == "do.pingcap.net":
            canonical_host = IDC_HOST
        elif host == "jenkins.jenkins.svc.cluster.local":
            canonical_host = GCP_HOST
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
        return _canonical_full_url(path, canonical_host=canonical_host or GCP_HOST)

    if any(normalized.startswith(prefix) for prefix in INTERNAL_JENKINS_HOST_PREFIXES) and path.startswith(CANONICAL_JENKINS_PATH_PREFIX):
        return _canonical_full_url(path, canonical_host=GCP_HOST)
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
    host = GCP_HOST if str(cloud_phase or "").upper() == "GCP" else IDC_HOST
    return f"{host}{path}"


def canonicalize_job_name(value: str | None, *, repo_full_name: str | None = None) -> str | None:
    if value is None:
        return None

    normalized = value.strip().strip("/")
    if normalized == "":
        return None
    if "/" in normalized:
        return normalized
    if repo_full_name and " " not in normalized:
        repo_prefix = repo_full_name.strip().strip("/")
        if repo_prefix:
            return f"{repo_prefix}/{normalized}"
    return normalized


def full_job_name_to_normalized_jenkins_job_path(full_job_name: str | None) -> str | None:
    normalized = canonicalize_job_name(full_job_name)
    if normalized is None:
        return None

    segments = [segment for segment in normalized.split("/") if segment]
    if len(segments) < 3:
        return None

    org, repo, *job_segments = segments
    if not job_segments:
        return None

    path_segments = ["jenkins", "job", org, "job", repo]
    for segment in job_segments:
        path_segments.extend(("job", segment))
    return "/" + "/".join(path_segments) + "/"


def _canonical_full_url(path: str, *, canonical_host: str) -> str:
    normalized_path = path.rstrip("/")
    if normalized_path == "":
        return f"{canonical_host}/"
    return f"{canonical_host}{normalized_path}/"
