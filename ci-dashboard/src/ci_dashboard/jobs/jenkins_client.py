from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from ci_dashboard.common.config import JenkinsSettings


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


def build_progressive_text_url(build_url: str, *, internal_base_url: str | None = None) -> str:
    normalized_build_url = canonicalize_build_url(build_url)
    if internal_base_url:
        normalized_build_url = rewrite_build_url_host(
            normalized_build_url,
            internal_base_url=internal_base_url,
        )
    return f"{normalized_build_url}logText/progressiveText"


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
    rewritten = urlunsplit(
        (
            target.scheme or source.scheme,
            target.netloc or source.netloc,
            source.path,
            source.query,
            source.fragment,
        )
    )
    if not rewritten.endswith("/"):
        rewritten = f"{rewritten}/"
    return rewritten
