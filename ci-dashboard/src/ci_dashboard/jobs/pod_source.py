"""Collector identity rules shared by live watching and saved-Pod reconciliation."""

from __future__ import annotations

import re

from ci_dashboard.jobs.build_url_matcher import GCP_HOST, TENCENT_HOST

WATCH_JOB_NAME = "ci-watch-pods"
TENCENT_SOURCE_RE = re.compile(r"tencent:([a-z][a-z0-9]*(?:-[a-z0-9]+)+):(cls-[a-z0-9]+)\Z")


def cloud_phase_for_source(source_project: str) -> str:
    if not source_project or source_project != source_project.strip():
        raise ValueError("Pod source must be nonblank without surrounding whitespace")
    if len(source_project) > 128 or any(ord(char) < 32 for char in source_project):
        raise ValueError("Pod source exceeds the 128-character identity limit or contains control characters")
    if source_project.lower().startswith("tencent:"):
        if not TENCENT_SOURCE_RE.fullmatch(source_project):
            raise ValueError("Tencent Pod source must have the form tencent:<region>:cls-<id>")
        if len(f"{WATCH_JOB_NAME}:{source_project}") > 64:
            raise ValueError("Tencent Pod source exceeds the 64-character watcher job-state key limit")
        return "TENCENT"
    return "GCP"


def jenkins_host_for_cloud(cloud_phase: str) -> str:
    if cloud_phase == "GCP":
        return GCP_HOST
    if cloud_phase == "TENCENT":
        return TENCENT_HOST
    raise ValueError("Pod watcher cloud phase must be GCP or TENCENT")


def validate_watch_source(
    source_project: str,
    *,
    cloud_phase: str,
    cluster_name: str | None,
    location: str | None,
) -> None:
    jenkins_host_for_cloud(cloud_phase)
    if cloud_phase_for_source(source_project) != cloud_phase:
        raise ValueError("Pod source identity and configured cloud phase disagree")
    if cloud_phase == "TENCENT":
        if not cluster_name or not cluster_name.strip() or not location or not location.strip():
            raise ValueError("Tencent Pod watcher requires cluster name and location")
        if source_project.split(":")[1] != location:
            raise ValueError("Tencent Pod source region and configured location disagree")
