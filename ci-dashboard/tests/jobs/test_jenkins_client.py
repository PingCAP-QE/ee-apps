from __future__ import annotations

import gzip
import subprocess
import sys

import httpx
import pytest

from ci_dashboard.common.config import JenkinsSettings
from ci_dashboard.jobs.jenkins_client import (
    JenkinsClient,
    build_api_url,
    build_progressive_text_url,
    canonicalize_build_url,
    extract_console_signal_excerpts,
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


@pytest.mark.parametrize("host", ["do.pingcap.net", "external.example.com", "prow.tidb.net.example.com"])
@pytest.mark.parametrize("suffix", ["logText/progressiveText", "wfapi/describe", "consoleText", "timings/"])
def test_build_api_url_preserves_non_gcp_controller(host: str, suffix: str) -> None:
    assert build_api_url(
        f"https://{host}/jenkins/job/pingcap/job/tidb/job/ghpr_build/1201/display/redirect",
        suffix,
        internal_base_url="http://jenkins.jenkins.svc.cluster.local/jenkins",
    ) == f"https://{host}/jenkins/job/pingcap/job/tidb/job/ghpr_build/1201/{suffix}"


def test_build_progressive_text_url_preserves_tencent_controller() -> None:
    assert build_progressive_text_url(
        "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1193/",
        internal_base_url="http://jenkins.jenkins.svc.cluster.local",
    ) == "https://do.pingcap.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/1193/logText/progressiveText"


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


def test_jenkins_client_fetch_timings_html_uses_internal_url() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text="<table class='jenkins-table'></table>")

    client = JenkinsClient(
        JenkinsSettings(
            internal_base_url="http://jenkins.jenkins.svc.cluster.local:80",
            http_timeout_seconds=30,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        html = client.fetch_timings_html(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/",
            timeout_seconds=5,
        )
    finally:
        client.close()

    assert html == "<table class='jenkins-table'></table>"
    assert seen_urls == [
        "http://jenkins.jenkins.svc.cluster.local/jenkins/job/pingcap/job/tidb/job/ghpr_unit_test/301/timings/"
    ]


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


def test_extract_console_signal_excerpts_returns_kube_failure_context() -> None:
    console_text = "\n".join(
        [
            "random prefix",
            "still starting",
            "Pod xyz status update",
            "Container [golang] terminated [OOMKilled]",
            "removed or offline for 5 min",
            "Timeout waiting for agent to come back",
        ]
    )

    excerpts = extract_console_signal_excerpts(
        console_text,
        context_lines=1,
        max_matches=2,
        max_bytes=4096,
    )

    assert "===== Jenkins console signal excerpts =====" in excerpts
    assert "----- lines 3-5 -----" in excerpts
    assert "Pod xyz status update" in excerpts
    assert "Container [golang] terminated [OOMKilled]" in excerpts
    assert "removed or offline for 5 min" in excerpts
    assert "Timeout waiting for agent to come back" not in excerpts


def test_jenkins_client_fetch_console_signal_excerpts_uses_console_text_endpoint() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/consoleText"):
            return httpx.Response(
                200,
                text=(
                    "startup line\n"
                    "Pod status update\n"
                    "Container [golang] terminated [OOMKilled]\n"
                    "tail line\n"
                ),
            )
        return httpx.Response(404)

    client = JenkinsClient(
        JenkinsSettings(http_timeout_seconds=5),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        excerpts = client.fetch_console_signal_excerpts(
            "https://prow.tidb.net/jenkins/job/pingcap/job/tiflow/job/pull_cdc_storage_integration_light/1357/",
            max_bytes=4096,
        )
    finally:
        client.close()

    assert excerpts.startswith("===== Jenkins console signal excerpts =====")
    assert "Container [golang] terminated [OOMKilled]" in excerpts
    assert seen_paths == [
        "/jenkins/job/pingcap/job/tiflow/job/pull_cdc_storage_integration_light/1357/consoleText"
    ]


@pytest.mark.parametrize(
    "signal",
    [
        "Container golang was using 12Gi, request is 8Gi, larger consumption of memory",
        "CONTAINER golang WAS USING 12Gi, REQUEST IS 8Gi, LARGER CONSUMPTION OF MEMORY",
        "Container [golang] terminated [OOMKilled]",
        "Container [jnlp] terminated [Evicted]",
        "Pod [Failed][OOMKilled: exit 137]",
        "Pod [Failed][Evicted: resource pressure]",
        "Pod [Failed][TerminationByKubelet]",
        "Reason: OOMKilled",
        "Reason: Evicted",
        "The node was low on resource: memory",
    ],
)
def test_console_signals_preserve_middle_of_log(signal) -> None:
    log = "startup\n" * 500 + signal + "\n" + "cleanup\n" * 500
    excerpt = extract_console_signal_excerpts(log, context_lines=1)
    assert signal in excerpt
    assert "startup" in excerpt
    assert "cleanup" in excerpt


def test_console_memory_signal_requires_ordered_fragments() -> None:
    assert extract_console_signal_excerpts(
        "Container golang request is 8Gi was using 12Gi larger consumption of memory"
    ) == ""


def test_console_signal_scan_handles_adversarial_lines_with_bounded_runtime() -> None:
    # A subprocess deadline kills a reintroduced backtracking regex instead of hanging pytest.
    result = subprocess.run(
        [sys.executable, "-c", """
from ci_dashboard.jobs.jenkins_client import extract_console_signal_excerpts
for line in (
    'Container ' + ' was using request is ' * 4000,
    'Container [' * 10000,
    'Pod [Failed][' * 10000,
):
    assert extract_console_signal_excerpts(line) == ''
"""],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("prefix", ["x", "中"])
def test_console_signal_extraction_bounds_scanned_bytes_and_reports_truncation(prefix) -> None:
    excerpt = extract_console_signal_excerpts(
        prefix * 200 + "\nReason: OOMKilled\n",
        max_scan_bytes=128,
        max_bytes=96,
    )
    assert "scan truncated" in excerpt
    assert "OOMKilled" not in excerpt
    assert len(excerpt.encode("utf-8")) <= 96


def test_console_signal_excerpt_respects_utf8_output_byte_limit() -> None:
    excerpt = extract_console_signal_excerpts(
        "Reason: OOMKilled " + "中" * 100,
        max_bytes=91,
    )
    assert "Reason: OOMKilled" in excerpt
    assert len(excerpt.encode("utf-8")) <= 91


class _TrackedConsoleStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_bytes = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_bytes += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def test_console_signal_fetch_stops_reading_and_closes_oversized_response() -> None:
    signal = b"Reason: OOMKilled\n"
    stream = _TrackedConsoleStream([signal] + [b"x" * 16] * 10000)

    def handler(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = JenkinsClient(JenkinsSettings(), client=http_client)
        excerpt = client.fetch_console_signal_excerpts(
            "https://prow.tidb.net/jenkins/job/test/1/", max_scan_bytes=128,
        )

    assert "Reason: OOMKilled" in excerpt
    assert "scan truncated" in excerpt
    assert stream.read_bytes <= 128 + 16
    assert stream.closed


@pytest.mark.parametrize("body", [b"", b"x" * 128, "中".encode("utf-8") * 42 + b"xx"])
def test_console_signal_fetch_does_not_mark_complete_bounded_body_as_truncated(body) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    with httpx.Client(transport=transport) as http_client:
        client = JenkinsClient(JenkinsSettings(), client=http_client)
        assert client.fetch_console_signal_excerpts(
            "https://prow.tidb.net/jenkins/job/test/1/", max_scan_bytes=128,
        ) == ""


@pytest.mark.parametrize("limit", [0, -1])
def test_console_signal_scan_rejects_nonpositive_budgets_before_http(limit) -> None:
    def unexpected_request(request):
        pytest.fail("invalid scan budget reached HTTP")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as http_client:
        client = JenkinsClient(JenkinsSettings(), client=http_client)
        with pytest.raises(ValueError, match="max_scan_bytes"):
            client.fetch_console_signal_excerpts(
                "https://prow.tidb.net/jenkins/job/test/1/", max_scan_bytes=limit,
            )
    with pytest.raises(ValueError, match="max_scan_bytes"):
        extract_console_signal_excerpts("Reason: OOMKilled", max_scan_bytes=limit)


def test_console_signal_fetch_rejects_unexpected_compression_before_reading() -> None:
    stream = _TrackedConsoleStream([gzip.compress(b"x" * 100000)])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"}),
    )
    with httpx.Client(transport=transport) as http_client:
        client = JenkinsClient(JenkinsSettings(), client=http_client)
        with pytest.raises(httpx.DecodingError, match="Content-Encoding"):
            client.fetch_console_signal_excerpts("https://prow.tidb.net/jenkins/job/test/1/")

    assert stream.read_bytes == 0
    assert stream.closed


@pytest.mark.parametrize("status", [403, 500])
def test_console_signal_fetch_closes_http_error_without_reading(status) -> None:
    stream = _TrackedConsoleStream([b"error body"])
    transport = httpx.MockTransport(lambda request: httpx.Response(status, stream=stream))
    with httpx.Client(transport=transport) as http_client:
        client = JenkinsClient(JenkinsSettings(), client=http_client)
        with pytest.raises(httpx.HTTPStatusError):
            client.fetch_console_signal_excerpts("https://prow.tidb.net/jenkins/job/test/1/")
    assert stream.read_bytes == 0
    assert stream.closed
