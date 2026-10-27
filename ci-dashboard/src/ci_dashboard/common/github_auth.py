"""Client for the in-cluster GitHub token broker.

The broker (`github-token-broker`, PingCAP-QE/ee-apps) mints repository-scoped
GitHub App installation tokens for Kubernetes workloads. Callers authenticate
with their projected ServiceAccount token, so no long-lived PAT is required.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

LOG = logging.getLogger(__name__)

DEFAULT_BROKER_URL = "http://github-token-broker.github-token-broker.svc"
DEFAULT_SERVICE_ACCOUNT_TOKEN_FILE = (
    "/var/run/secrets/kubernetes.io/serviceaccount/token"
)
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_SAFETY_MARGIN_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
# Mirrors the Go client: a response without expires_at is treated as one hour
# minus a small margin.
DEFAULT_EXPIRES_IN_SECONDS = 50 * 60
MAX_ERROR_BODY_CHARS = 200


class GitHubTokenError(RuntimeError):
    """Raised when the token broker cannot mint a token."""


class GitHubTokenNotInstalled(GitHubTokenError):
    """Raised when the GitHub App has no installation for the repository owner.

    This is a coverage boundary, not a failure: forks and third-party
    repositories are outside the App installation, and callers usually skip
    those rows instead of aborting the whole sync.
    """


@dataclass(frozen=True)
class GitHubAuthSettings:
    broker_url: str = DEFAULT_BROKER_URL
    token_file: str = DEFAULT_SERVICE_ACCOUNT_TOKEN_FILE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    safety_margin_seconds: int = DEFAULT_SAFETY_MARGIN_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    # Static PAT used only when CI_DASHBOARD_GITHUB_PAT_FALLBACK is enabled.
    fallback_token: str | None = None


@dataclass(frozen=True)
class GitHubTokenRequest:
    """A repository-scoped permission request derived from a GitHub API URL."""

    repo: str
    permissions: Mapping[str, str]

    @property
    def cache_key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (self.repo, tuple(sorted(self.permissions.items())))


@dataclass(frozen=True)
class _MintedToken:
    token: str
    expires_at: datetime


def token_request_for_url(url: str) -> GitHubTokenRequest | None:
    """Map a GitHub API URL to the narrowest broker permission request.

    Returns ``None`` for URLs that the broker cannot scope (non-GitHub hosts or
    resources without a repository), in which case no Authorization header is
    added.
    """

    parsed = urlsplit(url)
    if parsed.hostname != "api.github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "repos":
        return None

    owner, repo = parts[1], parts[2]
    resource = parts[3:]
    if "pulls" in resource:
        return GitHubTokenRequest(f"{owner}/{repo}", {"pull_requests": "read"})
    if "issues" in resource:
        return GitHubTokenRequest(f"{owner}/{repo}", {"issues": "write"})
    return None


class GitHubTokenBroker:
    """Mints and caches repository-scoped GitHub App installation tokens."""

    def __init__(
        self,
        settings: GitHubAuthSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[str, float]] = {}
        self._warned_missing_caller_token = False

    def token_for(self, request: GitHubTokenRequest) -> str | None:
        """Return a scoped token, a fallback PAT, or ``None`` for anonymous calls."""

        if not self._settings.broker_url.strip():
            return self._fallback_or_none(request, reason="token broker is disabled")

        cached = self._cache.get(request.cache_key)
        if cached is not None and cached[1] > self._monotonic():
            return cached[0]

        caller_token = self._read_caller_token()
        if caller_token is None:
            return self._fallback_or_none(
                request,
                reason="service account token is not available",
            )

        try:
            minted = self._mint(request, caller_token)
        except GitHubTokenError as exc:
            if self._settings.fallback_token:
                LOG.warning(
                    "github token broker mint failed; falling back to static token",
                    extra={
                        "repo": request.repo,
                        "permissions": dict(request.permissions),
                        "error": str(exc),
                    },
                )
                return self._settings.fallback_token
            raise

        ttl_seconds = (
            minted.expires_at - self._now()
        ).total_seconds() - self._settings.safety_margin_seconds
        if ttl_seconds > 0:
            self._cache[request.cache_key] = (
                minted.token,
                self._monotonic() + ttl_seconds,
            )
        LOG.info(
            "minted scoped GitHub token from broker",
            extra={
                "repo": request.repo,
                "permissions": dict(request.permissions),
                "expires_at": minted.expires_at.isoformat(),
                "cached_seconds": max(0, int(ttl_seconds)),
            },
        )
        return minted.token

    def _read_caller_token(self) -> str | None:
        try:
            with open(self._settings.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError:
            token = ""
        if token:
            return token
        if not self._warned_missing_caller_token:
            self._warned_missing_caller_token = True
            LOG.warning(
                "service account token not found; skipping GitHub token broker",
                extra={"token_file": self._settings.token_file},
            )
        return None

    def _fallback_or_none(self, request: GitHubTokenRequest, *, reason: str) -> str | None:
        if self._settings.fallback_token:
            LOG.warning(
                "using static GitHub token fallback",
                extra={
                    "reason": reason,
                    "repo": request.repo,
                    "permissions": dict(request.permissions),
                },
            )
            return self._settings.fallback_token
        LOG.debug(
            "no GitHub token available for request",
            extra={"reason": reason, "repo": request.repo},
        )
        return None

    def _mint(self, request: GitHubTokenRequest, caller_token: str) -> _MintedToken:
        payload = {"repo": request.repo, "permissions": dict(request.permissions)}
        headers = {
            "Authorization": f"Bearer {caller_token}",
            "Content-Type": "application/json",
        }
        endpoint = f"{self._settings.broker_url.rstrip('/')}/v1/token"
        attempts = max(1, self._settings.max_attempts)
        last_error = "unknown error"
        last_status: int | None = None

        with httpx.Client(
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
        ) as client:
            for attempt in range(attempts):
                retryable = False
                try:
                    response = client.post(endpoint, headers=headers, json=payload)
                except httpx.TransportError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    retryable = True
                else:
                    if response.status_code == httpx.codes.OK:
                        return self._parse_minted_token(response)
                    body = response.text.strip()[:MAX_ERROR_BODY_CHARS]
                    last_error = f"HTTP {response.status_code}: {body}"
                    last_status = response.status_code
                    retryable = response.status_code >= 500

                LOG.warning(
                    "github token broker request failed",
                    extra={
                        "repo": request.repo,
                        "permissions": dict(request.permissions),
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                        "retryable": retryable,
                        "error": last_error,
                    },
                )
                if not retryable or attempt + 1 >= attempts:
                    break
                self._sleep(self._settings.retry_backoff_seconds * (attempt + 1))

        if last_status == httpx.codes.NOT_FOUND:
            raise GitHubTokenNotInstalled(
                f"github app is not installed for {request.repo}: {last_error}"
            )
        raise GitHubTokenError(
            f"token broker request failed for {request.repo}: {last_error}"
        )

    def _parse_minted_token(self, response: httpx.Response) -> _MintedToken:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubTokenError(
                "token broker returned a non-JSON response"
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubTokenError("token broker returned an invalid response")
        token = str(payload.get("token") or "").strip()
        if not token:
            raise GitHubTokenError("token broker returned an empty token")
        expires_at = _parse_expires_at(payload.get("expires_at"))
        if expires_at is None:
            expires_at = self._now() + timedelta(seconds=DEFAULT_EXPIRES_IN_SECONDS)
        return _MintedToken(token=token, expires_at=expires_at)


def _parse_expires_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_bool(environ: Mapping[str, str], key: str, default: bool) -> bool:
    raw = environ.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def _read_positive_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def load_github_auth_settings(
    environ: Mapping[str, str] | None = None,
) -> GitHubAuthSettings:
    """Build broker settings from environment variables.

    - ``CI_DASHBOARD_GITHUB_BROKER_URL`` (default: in-cluster broker service;
      an empty value disables the broker)
    - ``CI_DASHBOARD_GITHUB_BROKER_TOKEN_FILE`` (default: projected SA token)
    - ``CI_DASHBOARD_GITHUB_BROKER_TIMEOUT_SECONDS`` (default: 10)
    - ``CI_DASHBOARD_GITHUB_PAT_FALLBACK`` + ``GITHUB_TOKEN``: explicit, logged
      fallback used only when the switch is enabled
    """

    env = os.environ if environ is None else environ
    raw_broker_url = env.get("CI_DASHBOARD_GITHUB_BROKER_URL")
    broker_url = (
        DEFAULT_BROKER_URL if raw_broker_url is None else raw_broker_url.strip()
    )
    token_file = (
        env.get("CI_DASHBOARD_GITHUB_BROKER_TOKEN_FILE") or ""
    ).strip() or DEFAULT_SERVICE_ACCOUNT_TOKEN_FILE
    fallback_token = None
    if _read_bool(env, "CI_DASHBOARD_GITHUB_PAT_FALLBACK", False):
        fallback_token = (env.get("GITHUB_TOKEN") or "").strip() or None

    return GitHubAuthSettings(
        broker_url=broker_url,
        token_file=token_file,
        timeout_seconds=_read_positive_int(
            env,
            "CI_DASHBOARD_GITHUB_BROKER_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        fallback_token=fallback_token,
    )
