from __future__ import annotations

import httpx

from ci_dashboard.common.config import JenkinsSettings
from ci_dashboard.jobs.jenkins_client import (
    JenkinsClient,
    build_progressive_text_url,
    canonicalize_build_url,
    rewrite_build_url_host,
)


def test_canonicalize_build_url_strips_display_redirect() -> None:
    assert (
        canonicalize_build_url(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/display/redirect"
        )
        == "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )


def test_build_progressive_text_url_rewrites_to_internal_host() -> None:
    url = build_progressive_text_url(
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
        internal_base_url="http://jenkins.jenkins.svc.cluster.local:80",
    )

    assert (
        url
        == "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/logText/progressiveText"
    )


def test_rewrite_build_url_host_preserves_path() -> None:
    assert (
        rewrite_build_url_host(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            internal_base_url="http://jenkins.jenkins.svc.cluster.local:80",
        )
        == "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )


def test_jenkins_client_fetch_console_tail_uses_progressive_text_probe_then_tail() -> None:
    full_log = "0123456789abcdefghijklmnopqrstuvwxyz"
    chunk_size = 8
    seen_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        seen_starts.append(start)
        if start >= len(full_log):
            return httpx.Response(
                200,
                text="",
                headers={"X-Text-Size": str(len(full_log)), "X-More-Data": "false"},
            )

        end = min(len(full_log), start + chunk_size)
        return httpx.Response(
            200,
            text=full_log[start:end],
            headers={
                "X-Text-Size": str(end),
                "X-More-Data": "true" if end < len(full_log) else "false",
            },
        )

    client = JenkinsClient(
        JenkinsSettings(progressive_probe_start=10_000, http_timeout_seconds=5),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        tail = client.fetch_console_tail(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            max_bytes=10,
        )
    finally:
        client.close()

    assert tail == full_log[-10:]
    assert seen_starts[0] == 10_000
    assert seen_starts[1] == len(full_log) - 10
