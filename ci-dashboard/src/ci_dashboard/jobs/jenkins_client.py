from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ci_dashboard.common.config import JenkinsSettings

DEFAULT_FAILED_NODE_LOG_LIMIT = 4
DEFAULT_FAILED_NODE_LOG_BYTES = 64 * 1024
CONSOLE_OUTPUT_RE = re.compile(r'<pre class="console-output">(.*?)</pre>', flags=re.DOTALL)


@dataclass(frozen=True)
class ProgressiveTextChunk:
    text: str
    next_start: int
    more_data: bool


class JenkinsClient:
    def __init__(self, settings: JenkinsSettings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        auth: tuple[str, str] | None = None
        if settings.username and settings.api_token:
            auth = (settings.username, settings.api_token)
        self._client = client or httpx.Client(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            auth=auth,
            headers={"Accept-Encoding": "gzip"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_console_tail(self, build_url: str, *, max_bytes: int) -> str:
        progressive_url = build_progressive_text_url(
            build_url,
            internal_base_url=self._settings.internal_base_url,
        )

        probe = self._fetch_progressive_chunk(progressive_url, self._settings.progressive_probe_start)
        desired_start = max(0, probe.next_start - max_bytes)
        chunk = self._fetch_progressive_chunk(progressive_url, desired_start)
        collected_parts = [chunk.text]
        next_start = chunk.next_start
        more_data = chunk.more_data

        while more_data:
            chunk = self._fetch_progressive_chunk(progressive_url, next_start)
            if chunk.next_start <= next_start:
                break
            collected_parts.append(chunk.text)
            next_start = chunk.next_start
            more_data = chunk.more_data

        combined = "".join(collected_parts)
        encoded = combined.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return combined
        return encoded[-max_bytes:].decode("utf-8", errors="replace")

    def fetch_failed_node_logs(
        self,
        build_url: str,
        *,
        max_nodes: int = DEFAULT_FAILED_NODE_LOG_LIMIT,
        max_bytes_per_node: int = DEFAULT_FAILED_NODE_LOG_BYTES,
    ) -> str:
        build_description = self._fetch_json(
            build_api_url(
                build_url,
                "wfapi/describe",
                internal_base_url=self._settings.internal_base_url,
            )
        )
        parts: list[str] = []
        seen_node_ids: set[str] = set()
        for stage in build_description.get("stages") or []:
            if not _is_failed_or_aborted(stage):
                continue
            stage_id = str(stage.get("id") or "").strip()
            if not stage_id:
                continue
            stage_description = self._fetch_json(
                build_api_url(
                    build_url,
                    f"execution/node/{stage_id}/wfapi/describe",
                    internal_base_url=self._settings.internal_base_url,
                )
            )
            for node in _failed_flow_nodes(stage_description):
                node_id = str(node.get("id") or "").strip()
                if not node_id or node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                node_log = self.fetch_node_log(build_url, node_id=node_id, max_bytes=max_bytes_per_node)
                if not node_log.strip():
                    continue
                parts.append(_format_failed_node_log_header(stage_description, node) + node_log)
                if len(parts) >= max_nodes:
                    return "\n".join(parts)
        return "\n".join(parts)

    def fetch_node_log(self, build_url: str, *, node_id: str, max_bytes: int) -> str:
        node_log_url = build_api_url(
            build_url,
            f"execution/node/{node_id}/wfapi/log",
            internal_base_url=self._settings.internal_base_url,
        )
        payload = self._fetch_json(node_log_url)
        text = str(payload.get("text") or "")
        if payload.get("hasMore"):
            console_text = self._fetch_node_console_text(build_url, node_id=node_id)
            if console_text.strip():
                text = console_text
        return _tail_text(text, max_bytes=max_bytes)

    def _fetch_progressive_chunk(self, progressive_url: str, start: int) -> ProgressiveTextChunk:
        response = self._client.get(progressive_url, params={"start": start})
        response.raise_for_status()
        next_start = int(response.headers.get("X-Text-Size", str(start)))
        more_data = response.headers.get("X-More-Data", "").strip().lower() == "true"
        return ProgressiveTextChunk(
            text=response.text,
            next_start=next_start,
            more_data=more_data,
        )

    def _fetch_json(self, url: str) -> dict[str, Any]:
        response = self._client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Jenkins API returned non-object JSON for {url}")
        return payload

    def _fetch_node_console_text(self, build_url: str, *, node_id: str) -> str:
        console_url = build_api_url(
            build_url,
            f"execution/node/{node_id}/log",
            internal_base_url=self._settings.internal_base_url,
        )
        response = self._client.get(console_url)
        response.raise_for_status()
        match = CONSOLE_OUTPUT_RE.search(response.text)
        raw_text = match.group(1) if match else response.text
        return html.unescape(re.sub(r"<[^>]+>", "", raw_text))


def build_progressive_text_url(build_url: str, *, internal_base_url: str | None = None) -> str:
    return build_api_url(build_url, "logText/progressiveText", internal_base_url=internal_base_url)


def build_api_url(
    build_url: str,
    suffix: str,
    *,
    internal_base_url: str | None = None,
) -> str:
    normalized_build_url = canonicalize_build_url(build_url)
    if internal_base_url:
        normalized_build_url = rewrite_build_url_host(
            normalized_build_url,
            internal_base_url=internal_base_url,
        )
    return f"{normalized_build_url}{suffix.lstrip('/')}"


def canonicalize_build_url(build_url: str) -> str:
    normalized = build_url.strip()
    if normalized.endswith("/display/redirect"):
        normalized = normalized[: -len("/display/redirect")]
    if not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def rewrite_build_url_host(build_url: str, *, internal_base_url: str) -> str:
    source = urlsplit(build_url)
    target = urlsplit(internal_base_url)
    source_path = source.path or "/"
    target_path = target.path.rstrip("/")
    if target_path and not (
        source_path == target_path or source_path.startswith(f"{target_path}/")
    ):
        source_path = f"{target_path}{source_path if source_path.startswith('/') else '/' + source_path}"
    rewritten = urlunsplit(
        (
            target.scheme or source.scheme,
            target.netloc or source.netloc,
            source_path,
            source.query,
            source.fragment,
        )
    )
    if not rewritten.endswith("/"):
        rewritten = f"{rewritten}/"
    return rewritten


def _failed_flow_nodes(stage_description: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = stage_description.get("stageFlowNodes")
    if isinstance(nodes, list):
        failed_nodes = [
            node
            for node in nodes
            if isinstance(node, dict) and _is_failed_or_aborted(node)
        ]
        if failed_nodes:
            return failed_nodes
    if _is_failed_or_aborted(stage_description):
        return [stage_description]
    return []


def _is_failed_or_aborted(node: dict[str, Any]) -> bool:
    return str(node.get("status") or "").upper() in {"FAILED", "ABORTED"}


def _tail_text(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="replace")


def _format_failed_node_log_header(stage: dict[str, Any], node: dict[str, Any]) -> str:
    stage_name = str(stage.get("name") or "<unknown stage>")
    stage_id = str(stage.get("id") or "<unknown>")
    node_name = str(node.get("name") or "<unknown node>")
    node_id = str(node.get("id") or "<unknown>")
    parameter = str(node.get("parameterDescription") or "").strip()
    header = (
        "\n\n===== Jenkins failed pipeline node log =====\n"
        f"stage: {stage_name} ({stage_id})\n"
        f"node: {node_name} ({node_id})\n"
    )
    if parameter:
        header += f"parameter: {parameter}\n"
    return f"{header}----- log -----\n"
