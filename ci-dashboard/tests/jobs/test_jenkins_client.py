from __future__ import annotations

import httpx

from ci_dashboard.common.config import JenkinsSettings
from ci_dashboard.jobs.jenkins_client import (
    JenkinsClient,
    build_api_url,
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


def test_build_api_url_rewrites_to_internal_host() -> None:
    url = build_api_url(
        "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/display/redirect",
        "wfapi/describe",
        internal_base_url="http://jenkins.jenkins.svc.cluster.local:80",
    )

    assert (
        url
        == "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/wfapi/describe"
    )


def test_rewrite_build_url_host_preserves_path() -> None:
    assert (
        rewrite_build_url_host(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            internal_base_url="http://jenkins.jenkins.svc.cluster.local:80",
        )
        == "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )


def test_rewrite_build_url_host_applies_internal_base_subpath_without_duplication() -> None:
    assert (
        rewrite_build_url_host(
            "https://external.example.com/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            internal_base_url="http://jenkins.jenkins.svc.cluster.local:80/jenkins",
        )
        == "http://jenkins.jenkins.svc.cluster.local:80/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/"
    )

    assert (
        rewrite_build_url_host(
            "https://external.example.com/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            internal_base_url="http://jenkins.jenkins.svc.cluster.local:80/jenkins",
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


def test_jenkins_client_fetch_failed_node_logs_collects_failed_stage_flow_nodes() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/execution/node/22/wfapi/describe"):
            return httpx.Response(
                200,
                json={
                    "id": "22",
                    "name": "Test",
                    "status": "FAILED",
                    "stageFlowNodes": [
                        {"id": "31", "name": "Shell Script", "status": "SUCCESS"},
                        {
                            "id": "32",
                            "name": "Shell Script",
                            "status": "FAILED",
                            "parameterDescription": "run_real_tikv_tests.sh bazel_importintotest",
                        },
                    ],
                },
            )
        if request.url.path.endswith("/execution/node/32/wfapi/log"):
            return httpx.Response(
                200,
                json={
                    "nodeId": "32",
                    "nodeStatus": "FAILED",
                    "length": 128,
                    "hasMore": False,
                    "text": "INFO: Build completed, 1 test FAILED, 5247 total actions\n",
                },
            )
        if request.url.path.endswith("/wfapi/describe"):
            return httpx.Response(
                200,
                json={
                    "stages": [
                        {"id": "11", "name": "Checkout", "status": "SUCCESS"},
                        {"id": "22", "name": "Test", "status": "FAILED"},
                    ]
                },
            )
        return httpx.Response(404)

    client = JenkinsClient(
        JenkinsSettings(http_timeout_seconds=5),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        text = client.fetch_failed_node_logs(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/2567/",
        )
    finally:
        client.close()

    assert "stage: Test (22)" in text
    assert "node: Shell Script (32)" in text
    assert "run_real_tikv_tests.sh bazel_importintotest" in text
    assert "Build completed, 1 test FAILED" in text
    assert any(path.endswith("/execution/node/32/wfapi/log") for path in seen_paths)


def test_jenkins_client_fetch_failed_node_logs_collects_aborted_matrix_node_console_tail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/execution/node/44/wfapi/describe"):
            return httpx.Response(
                200,
                json={
                    "id": "44",
                    "name": "Test",
                    "status": "ABORTED",
                    "stageFlowNodes": [
                        {
                            "id": "55",
                            "name": "G11",
                            "status": "ABORTED",
                            "parameterDescription": "TEST_GROUP = 'G11'",
                        },
                    ],
                },
            )
        if request.url.path.endswith("/execution/node/55/wfapi/log"):
            return httpx.Response(
                200,
                json={
                    "nodeId": "55",
                    "nodeStatus": "FAILED",
                    "length": 10240,
                    "hasMore": True,
                    "text": "first chunk without the case failure\n",
                },
            )
        if request.url.path.endswith("/execution/node/55/log"):
            return httpx.Response(
                200,
                text=(
                    '<pre class="console-output">'
                    "prefix that should be trimmed\n"
                    "TEST FAILED: OUTPUT DOES NOT CONTAIN 'id: 1'\n"
                    "suffix\n"
                    "</pre>"
                ),
            )
        if request.url.path.endswith("/wfapi/describe"):
            return httpx.Response(
                200,
                json={
                    "stages": [
                        {
                            "id": "44",
                            "name": "Test",
                            "status": "ABORTED",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    client = JenkinsClient(
        JenkinsSettings(http_timeout_seconds=5),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        text = client.fetch_failed_node_logs(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_cdc_integration_storage_test/166/",
            max_bytes_per_node=128,
        )
    finally:
        client.close()

    assert "stage: Test (44)" in text
    assert "node: G11 (55)" in text
    assert "TEST FAILED: OUTPUT DOES NOT CONTAIN" in text
