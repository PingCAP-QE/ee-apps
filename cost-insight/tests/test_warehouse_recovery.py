"""Warehouse owner recovery uses only bounded, authenticated in-cluster Pod reads."""

import json
import ssl
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import pytest

from cost_insight.jobs import warehouse_recovery as recovery

OWNER = {"pod_name": "warehouse-run-123", "pod_namespace": "cost-insight", "pod_uid": "old-uid"}


def pod(phase="Running", uid="old-uid"):
    return {"metadata": {"name": OWNER["pod_name"], "namespace": OWNER["pod_namespace"], "uid": uid},
            "status": {"phase": phase}}


def response(body):
    result = BytesIO(json.dumps(body).encode())
    result.status = 200
    return result


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT_HTTPS", raising=False)
    monkeypatch.delenv("POD_NAMESPACE", raising=False)
    (tmp_path / "token").write_text("test-token\n")
    (tmp_path / "namespace").write_text("cost-insight\n")
    (tmp_path / "ca.crt").write_text("mock-ca")
    monkeypatch.setattr(recovery, "_SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(recovery.ssl, "create_default_context", Mock())
    opener = Mock()
    opener.open.return_value = response(pod("Succeeded"))
    monkeypatch.setattr(recovery, "build_opener", Mock(return_value=opener))
    return opener


def test_default_service_account_location():
    assert recovery._SERVICE_ACCOUNT == Path("/var/run/secrets/kubernetes.io/serviceaccount")


def test_only_authenticated_get_to_named_pod_with_verified_tls_and_timeout(cluster, tmp_path):
    assert recovery.assert_owner_stopped(OWNER) is None
    request = cluster.open.call_args.args[0]
    assert request.full_url == (
        "https://10.0.0.1:443/api/v1/namespaces/cost-insight/pods/warehouse-run-123")
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("Accept") == "application/json"
    cluster.open.assert_called_once_with(request, timeout=10)
    recovery.ssl.create_default_context.assert_called_once_with(cafile=str(tmp_path / "ca.crt"))
    handlers = recovery.build_opener.call_args.args
    proxy = next(h for h in handlers if isinstance(h, ProxyHandler))
    assert proxy.proxies == {}
    tls = next(h for h in handlers if isinstance(h, HTTPSHandler))
    assert tls._context is recovery.ssl.create_default_context.return_value
    assert any(isinstance(h, recovery._NoRedirect) for h in handlers)


@pytest.mark.parametrize("proxy_env", ["HTTPS_PROXY", "https_proxy"])
def test_real_urllib_ignores_ambient_proxies(cluster, monkeypatch, proxy_env):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(proxy_env, "http://proxy.example:3128")
    wire_response = addinfourl(BytesIO(json.dumps(pod("Succeeded")).encode()), {},
                               "https://10.0.0.1:443/", code=200)
    wire_response.msg = "OK"
    transport = HTTPSHandler()
    transport.https_open = Mock(return_value=wire_response)
    monkeypatch.setattr(recovery, "build_opener", lambda *handlers: build_opener(
        transport, *[h for h in handlers if not isinstance(h, HTTPSHandler)]))
    assert recovery.assert_owner_stopped(OWNER) is None
    request = transport.https_open.call_args.args[0]
    assert request.host == "10.0.0.1:443"
    assert request._tunnel_host is None


def test_404_proves_previous_pod_gone(cluster):
    cluster.open.side_effect = HTTPError("https://kubernetes", 404, "Not Found", {}, None)
    assert recovery.assert_owner_stopped(OWNER) is None
    cluster.open.assert_called_once()


@pytest.mark.parametrize("phase", ["Running", "Pending", "Unknown", None])
def test_different_uid_proves_previous_pod_gone_even_if_replacement_running(cluster, phase):
    replacement = pod(phase, uid="replacement-uid")
    replacement["metadata"]["deletionTimestamp"] = "2026-11-25T00:00:00Z"
    cluster.open.return_value = response(replacement)
    assert recovery.assert_owner_stopped(OWNER) is None


def test_different_uid_does_not_require_status_of_replacement(cluster):
    replacement = pod(uid="replacement-uid")
    del replacement["status"]
    cluster.open.return_value = response(replacement)
    assert recovery.assert_owner_stopped(OWNER) is None


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
@pytest.mark.parametrize("legacy", [False, True])
def test_only_terminal_phases_prove_same_uid_or_legacy_name_stopped(cluster, phase, legacy):
    cluster.open.return_value = response(pod(phase))
    owner = {k: v for k, v in OWNER.items() if not legacy or k != "pod_uid"}
    assert recovery.assert_owner_stopped(owner) is None


@pytest.mark.parametrize("phase", ["Running", "Pending", "Unknown", None, "", "succeeded", [], {}])
@pytest.mark.parametrize("legacy", [False, True])
def test_nonterminal_or_unknown_phase_refused_including_deleting_pod(cluster, phase, legacy):
    live = pod(phase)
    live["metadata"]["deletionTimestamp"] = "2026-11-25T00:00:00Z"
    cluster.open.return_value = response(live)
    owner = {k: v for k, v in OWNER.items() if not legacy or k != "pod_uid"}
    with pytest.raises(ValueError, match="not proven stopped"):
        recovery.assert_owner_stopped(owner)


def test_legacy_owner_cannot_prove_name_reuse_means_stopped(cluster):
    cluster.open.return_value = response(pod("Running", uid="replacement-uid"))
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped({"pod_name": OWNER["pod_name"]})


@pytest.mark.parametrize("status", [None, [], {}, {"reason": "Evicted"}])
def test_missing_phase_is_not_proof(cluster, status):
    live = pod()
    live["status"] = status
    cluster.open.return_value = response(live)
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308, 401, 403, 429, 500, 503])
def test_api_errors_fail_closed_without_retry(cluster, code):
    cluster.open.side_effect = HTTPError("https://kubernetes", code, "API error", {}, None)
    with pytest.raises(ValueError, match=f"HTTP {code}"):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_called_once()


@pytest.mark.parametrize("error", [TimeoutError("timed out"), URLError("unreachable"),
                                  OSError("connection reset"), IncompleteRead(b"partial"),
                                  ssl.SSLError("bad certificate")])
def test_transport_errors_fail_closed_without_retry(cluster, error):
    cluster.open.side_effect = error
    with pytest.raises(ValueError, match="lookup failed"):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_called_once()


@pytest.mark.parametrize("body", [b"not-json", b"", b"{", b"\xff"])
def test_invalid_json_fails_closed(cluster, body):
    stream = BytesIO(body)
    stream.status = 200
    cluster.open.return_value = stream
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)


@pytest.mark.parametrize("body", [None, [], "pod", {}, {"metadata": None}, {"metadata": []}])
def test_invalid_response_shape_fails_closed(cluster, body):
    cluster.open.return_value = response(body)
    with pytest.raises(ValueError, match="identity"):
        recovery.assert_owner_stopped(OWNER)


@pytest.mark.parametrize("field,value", [("name", None), ("name", "other-pod"),
    ("namespace", None), ("namespace", "other-namespace"), ("uid", None), ("uid", ""),
    ("uid", []), ("uid", "bad uid"), ("uid", "uid\n"), ("uid", 123)])
def test_response_identity_must_be_complete_and_match_lookup(cluster, field, value):
    live = pod("Succeeded")
    live["metadata"][field] = value
    cluster.open.return_value = response(live)
    with pytest.raises(ValueError, match="identity"):
        recovery.assert_owner_stopped(OWNER)


@pytest.mark.parametrize("status", [201, 204, 304])
def test_unexpected_success_status_is_not_proof(cluster, status):
    cluster.open.return_value.status = status
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)


@pytest.mark.parametrize("progress", [None, [], {}, {"run_id": "old"}, {"pod_name": None}])
def test_missing_owner_identity_fails_before_network(cluster, progress):
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(progress)
    cluster.open.assert_not_called()


@pytest.mark.parametrize("name", ["", "../pod", "pod/name", "pod?watch=true", "pod#fragment",
    "pod%2fother", "pod\n", "UPPER", "-pod", "pod-", "a..b", "a" * 64,
    ".".join(["a" * 63] * 4), 123])
def test_invalid_pod_name_cannot_change_url(cluster, name):
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped({**OWNER, "pod_name": name})
    cluster.open.assert_not_called()


@pytest.mark.parametrize("namespace", ["", "../ns", "ns/pods/other", "ns?watch=true", "UPPER",
                                        "ns.example", "a" * 64, [], 123])
def test_invalid_stored_namespace_cannot_change_url(cluster, namespace):
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped({**OWNER, "pod_namespace": namespace})
    cluster.open.assert_not_called()


@pytest.mark.parametrize("uid", ["", "bad uid", "uid\n", [], 123])
def test_invalid_stored_uid_fails_before_network(cluster, uid):
    with pytest.raises(ValueError, match="pod_uid"):
        recovery.assert_owner_stopped({**OWNER, "pod_uid": uid})
    cluster.open.assert_not_called()


def test_stored_namespace_takes_precedence_over_own_namespace(cluster, monkeypatch, tmp_path):
    monkeypatch.setenv("POD_NAMESPACE", "other-namespace")
    (tmp_path / "namespace").unlink()
    recovery.assert_owner_stopped(OWNER)
    assert "/namespaces/cost-insight/" in cluster.open.call_args.args[0].full_url


@pytest.mark.parametrize("use_env", [False, True])
def test_legacy_name_only_uses_own_namespace_from_env_or_service_account(
        cluster, monkeypatch, tmp_path, use_env):
    if use_env:
        monkeypatch.setenv("POD_NAMESPACE", "cost-insight")
        (tmp_path / "namespace").unlink()
    assert recovery.assert_owner_stopped({"pod_name": OWNER["pod_name"]}) is None
    assert "/namespaces/cost-insight/" in cluster.open.call_args.args[0].full_url


@pytest.mark.parametrize("use_env", [False, True])
def test_invalid_fallback_namespace_fails_closed(cluster, monkeypatch, tmp_path, use_env):
    if use_env:
        monkeypatch.setenv("POD_NAMESPACE", "../bad")
    else:
        (tmp_path / "namespace").write_text("../bad")
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped({"pod_name": OWNER["pod_name"]})
    cluster.open.assert_not_called()


def test_missing_fallback_namespace_fails_closed(cluster, tmp_path):
    (tmp_path / "namespace").unlink()
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped({"pod_name": OWNER["pod_name"]})
    cluster.open.assert_not_called()


@pytest.mark.parametrize("host", ["", "https://api", "api/evil", "api@evil", "api\n", "bad:ipv6"])
def test_invalid_api_host_fails_before_network(cluster, monkeypatch, host):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


@pytest.mark.parametrize("port", ["", "http", "0", "65536", "443/path"])
def test_invalid_api_port_fails_before_network(cluster, monkeypatch, port):
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", port)
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


def test_missing_api_host_fails_closed(cluster, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


@pytest.mark.parametrize("host,expected", [("kubernetes.default.svc", "kubernetes.default.svc"),
                                          ("2001:db8::1", "[2001:db8::1]")])
def test_service_dns_or_ipv6_and_configured_https_port(cluster, monkeypatch, host, expected):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    recovery.assert_owner_stopped(OWNER)
    assert cluster.open.call_args.args[0].full_url.startswith(f"https://{expected}:6443/")


@pytest.mark.parametrize("token", ["", "\n", "token\nInjected: header", "token with spaces"])
def test_invalid_token_fails_before_network(cluster, tmp_path, token):
    (tmp_path / "token").write_text(token)
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


def test_missing_token_fails_before_network(cluster, tmp_path):
    (tmp_path / "token").unlink()
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


@pytest.mark.parametrize("setting,value,expected", [
    ("POD_NAMESPACE", "../bad", "pod_namespace"),
    ("KUBERNETES_SERVICE_HOST", "api/evil", "service host"),
    ("KUBERNETES_SERVICE_PORT_HTTPS", "bad-port", "HTTPS port"),
])
def test_config_errors_preserve_specific_actionable_message(cluster, monkeypatch, setting, value, expected):
    monkeypatch.setenv(setting, value)
    owner = {"pod_name": OWNER["pod_name"]} if setting == "POD_NAMESPACE" else OWNER
    with pytest.raises(ValueError, match=expected):
        recovery.assert_owner_stopped(owner)
    cluster.open.assert_not_called()


def test_missing_token_retains_actionable_message_without_token_contents(cluster, tmp_path):
    (tmp_path / "token").unlink()
    with pytest.raises(ValueError, match="service account token"):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


def test_missing_or_invalid_ca_fails_before_network(cluster):
    recovery.ssl.create_default_context.side_effect = ssl.SSLError("invalid ca.crt")
    with pytest.raises(ValueError):
        recovery.assert_owner_stopped(OWNER)
    cluster.open.assert_not_called()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_real_urllib_redirect_handling_never_forwards_token(cluster, monkeypatch, code):
    redirect = addinfourl(BytesIO(b""), {"location": "https://untrusted.example/token"},
                         "https://10.0.0.1/", code=code)
    redirect.msg = "Redirect"
    transport = HTTPSHandler()
    transport.https_open = Mock(side_effect=[redirect, response(pod("Succeeded"))])
    # Exercise urllib's real error/redirect chain without a socket or live credentials.
    monkeypatch.setattr(recovery, "build_opener", lambda *handlers: build_opener(
        transport, *[h for h in handlers if not isinstance(h, HTTPSHandler)]))
    with pytest.raises(ValueError, match=f"HTTP {code}"):
        recovery.assert_owner_stopped(OWNER)
    transport.https_open.assert_called_once()
    assert transport.https_open.call_args.args[0].full_url.startswith("https://10.0.0.1:443/")
