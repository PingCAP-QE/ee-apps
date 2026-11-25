"""Fail-closed Kubernetes proof that a previous warehouse job owner has stopped."""

import ipaddress
import json
import os
import re
import ssl
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

_SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the service account bearer token to a redirect target.
        return None


def _valid_name(value, *, namespace=False):
    pattern = _LABEL if namespace else rf"{_LABEL}(?:\.{_LABEL})*"
    return (isinstance(value, str) and len(value) <= (63 if namespace else 253)
            and re.fullmatch(pattern, value) is not None)


def _valid_uid(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", value) is not None


def assert_owner_stopped(progress: dict) -> None:
    """Allow only an absent/replaced Pod or a terminal Pod, never an elapsed timeout.

    Legacy records without a UID require absence or a terminal live Pod. Callers
    must perform this network lookup outside their database transaction, then
    revalidate ownership under their own lock before reclaiming the checkpoint.
    """
    if not isinstance(progress, dict) or not _valid_name(progress.get("pod_name")):
        raise ValueError("Previous warehouse owner has no valid pod_name")
    name = progress["pod_name"]
    uid = progress.get("pod_uid")
    if uid is not None and not _valid_uid(uid):
        raise ValueError("Previous warehouse owner has no valid pod_uid")

    namespace = progress.get("pod_namespace")
    if namespace is None:
        try:
            namespace = os.getenv("POD_NAMESPACE") or (_SERVICE_ACCOUNT / "namespace").read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Cannot read warehouse owner pod_namespace") from exc
    if not _valid_name(namespace, namespace=True):
        raise ValueError("Previous warehouse owner has no valid pod_namespace")

    host = os.getenv("KUBERNETES_SERVICE_HOST", "")
    if ":" in host:
        try:
            host = f"[{ipaddress.IPv6Address(host)}]"
        except ValueError as exc:
            raise ValueError("Invalid Kubernetes service host") from exc
    elif not _valid_name(host):
        raise ValueError("Invalid Kubernetes service host")
    try:
        port = int(os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443"))
    except ValueError as exc:
        raise ValueError("Invalid Kubernetes HTTPS port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Invalid Kubernetes HTTPS port")
    try:
        token = (_SERVICE_ACCOUNT / "token").read_text().strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Cannot read Kubernetes service account token") from exc
    if not token or any(char.isspace() for char in token):
        raise ValueError("Missing or invalid Kubernetes service account token")
    try:
        context = ssl.create_default_context(cafile=str(_SERVICE_ACCOUNT / "ca.crt"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot load Kubernetes service account CA") from exc
    # Internal API calls must not inherit ambient proxy routing.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect())
    request = Request(f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods/{name}",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                      method="GET")
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("Unexpected Kubernetes Pod response status")
            pod = json.load(response)
    except HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return
        raise ValueError(f"Cannot prove warehouse owner stopped: Kubernetes HTTP {exc.code}") from exc
    except (OSError, HTTPException, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Cannot prove warehouse owner stopped: Kubernetes Pod lookup failed") from exc

    metadata = pod.get("metadata") if isinstance(pod, dict) else None
    if (not isinstance(metadata, dict) or metadata.get("name") != name
            or metadata.get("namespace") != namespace or not _valid_uid(metadata.get("uid"))):
        raise ValueError("Cannot prove warehouse owner stopped: invalid Kubernetes Pod identity")
    if uid is not None and metadata["uid"] != uid:
        return
    status = pod.get("status")
    if isinstance(status, dict) and status.get("phase") in ("Succeeded", "Failed"):
        return
    raise ValueError("Previous warehouse owner Pod is not proven stopped")
