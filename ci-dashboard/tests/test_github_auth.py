from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ci_dashboard.common.github_auth import (
    DEFAULT_BROKER_URL,
    DEFAULT_SERVICE_ACCOUNT_TOKEN_FILE,
    GitHubAuthSettings,
    GitHubTokenBroker,
    GitHubTokenError,
    load_github_auth_settings,
    token_request_for_url,
)


def _write_service_account_token(tmp_path: Path, value: str = "sa-token") -> Path:
    token_file = tmp_path / "serviceaccount" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(value, encoding="utf-8")
    return token_file


def _settings(tmp_path: Path, **overrides) -> GitHubAuthSettings:
    values = {
        "broker_url": "http://broker.test",
        "token_file": str(_write_service_account_token(tmp_path)),
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return GitHubAuthSettings(**values)


class _Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 10, 27, 8, 0, 0, tzinfo=timezone.utc)
        self.monotonic = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic_now(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.current = self.current + timedelta(seconds=seconds)


def _broker(
    handler,
    *,
    clock: _Clock | None = None,
    settings: GitHubAuthSettings | None = None,
    sleeps: list[float] | None = None,
) -> GitHubTokenBroker:
    clock = clock or _Clock()
    return GitHubTokenBroker(
        settings or GitHubAuthSettings(broker_url="http://broker.test", token_file="/dev/null"),
        transport=httpx.MockTransport(handler),
        now=clock.now,
        monotonic=clock.monotonic_now,
        sleep=(sleeps.append if sleeps is not None else (lambda _seconds: None)),
    )


def test_token_request_for_url_maps_github_resources() -> None:
    issues = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert issues is not None
    assert issues.repo == "pingcap/tidb"
    assert dict(issues.permissions) == {"issues": "write"}

    comments = token_request_for_url(
        "https://api.github.com/repos/tikv/pd/issues/comments/12345"
    )
    assert comments is not None
    assert comments.repo == "tikv/pd"
    assert dict(comments.permissions) == {"issues": "write"}

    pulls = token_request_for_url("https://api.github.com/repos/pingcap/tidb/pulls/7")
    assert pulls is not None
    assert pulls.repo == "pingcap/tidb"
    assert dict(pulls.permissions) == {"pull_requests": "read"}

    assert token_request_for_url("https://api.github.com/rate_limit") is None
    assert token_request_for_url("https://github.com/pingcap/tidb") is None
    assert token_request_for_url("https://api.github.com/repos/pingcap/tidb") is None


def test_token_for_mints_scoped_token_and_reuses_cache(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "payload": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={
                "token": "ghs_minted",
                "expires_at": "2026-10-27T09:00:00Z",
                "installation_id": 65036159,
                "source": "app",
            },
        )

    clock = _Clock()
    broker = _broker(handler, clock=clock, settings=_settings(tmp_path))
    request = token_request_for_url("https://api.github.com/repos/tikv/pd/issues/42")
    assert request is not None

    assert broker.token_for(request) == "ghs_minted"
    assert len(calls) == 1
    assert calls[0]["url"] == "http://broker.test/v1/token"
    assert calls[0]["authorization"] == "Bearer sa-token"
    assert calls[0]["payload"] == {
        "repo": "tikv/pd",
        "permissions": {"issues": "write"},
    }

    clock.advance(30)
    assert broker.token_for(request) == "ghs_minted"
    assert len(calls) == 1


def test_token_for_refreshes_before_expiry_margin(tmp_path: Path) -> None:
    issued = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        issued += 1
        return httpx.Response(
            200,
            json={
                "token": f"ghs_{issued}",
                "expires_at": (
                    "2026-10-27T08:10:00Z" if issued == 1 else "2026-10-27T10:00:00Z"
                ),
            },
        )

    clock = _Clock()
    broker = _broker(
        handler,
        clock=clock,
        settings=_settings(tmp_path, safety_margin_seconds=300),
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghs_1"
    # 10 minutes TTL minus the 5 minute safety margin still leaves a usable window.
    clock.advance(60)
    assert broker.token_for(request) == "ghs_1"
    # Inside the safety margin the token must be refreshed.
    clock.advance(5 * 60)
    assert broker.token_for(request) == "ghs_2"
    assert issued == 2


def test_token_for_does_not_cache_tokens_inside_the_safety_margin(tmp_path: Path) -> None:
    issued = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        issued += 1
        return httpx.Response(
            200,
            json={"token": f"ghs_{issued}", "expires_at": "2026-10-27T08:02:00Z"},
        )

    broker = _broker(handler, settings=_settings(tmp_path, safety_margin_seconds=300))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghs_1"
    assert broker.token_for(request) == "ghs_2"
    assert issued == 2


def test_token_for_retries_server_errors(tmp_path: Path) -> None:
    attempts: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("authorization", ""))
        if len(attempts) == 1:
            return httpx.Response(503, text="broker starting")
        return httpx.Response(
            200,
            json={"token": "ghs_ok", "expires_at": "2026-10-27T09:00:00Z"},
        )

    broker = _broker(
        handler,
        settings=_settings(tmp_path, max_attempts=2, retry_backoff_seconds=0.5),
        sleeps=sleeps,
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghs_ok"
    assert len(attempts) == 2
    assert sleeps == [0.5]


def test_token_for_does_not_retry_policy_denials(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"message": "permission denied by policy"})

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=3))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/pulls/9")
    assert request is not None

    with pytest.raises(GitHubTokenError) as excinfo:
        broker.token_for(request)

    assert attempts == 1
    assert "403" in str(excinfo.value)
    assert "permission denied by policy" in str(excinfo.value)
    assert "sa-token" not in str(excinfo.value)


def test_token_for_retries_timeouts_then_raises(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=2))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    with pytest.raises(GitHubTokenError, match="timed out|timeout"):
        broker.token_for(request)

    assert attempts == 2


def test_token_for_returns_none_without_service_account_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("broker must not be called without a caller token")

    broker = _broker(
        handler,
        settings=GitHubAuthSettings(
            broker_url="http://broker.test",
            token_file=str(tmp_path / "missing" / "token"),
        ),
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) is None


def test_token_for_falls_back_to_explicit_pat(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "denied"})

    broker = _broker(
        handler,
        settings=_settings(tmp_path, fallback_token="ghp_static", max_attempts=1),
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghp_static"


def test_token_for_uses_pat_when_broker_is_disabled(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("disabled broker must not be called")

    broker = _broker(
        handler,
        settings=GitHubAuthSettings(broker_url="", token_file="/dev/null", fallback_token="ghp_x"),
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghp_x"


def test_token_for_without_broker_or_pat_returns_none(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("disabled broker must not be called")

    broker = _broker(
        handler,
        settings=GitHubAuthSettings(broker_url="", token_file="/dev/null"),
    )
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) is None


def test_token_for_rejects_empty_token_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "", "expires_at": "2026-10-27T09:00:00Z"})

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=1))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    with pytest.raises(GitHubTokenError, match="empty token"):
        broker.token_for(request)


def test_token_for_errors_never_leak_minted_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=1))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    with pytest.raises(GitHubTokenError) as excinfo:
        broker.token_for(request)
    assert "sa-token" not in str(excinfo.value)


def test_load_github_auth_settings_defaults_and_env_overrides() -> None:
    defaults = load_github_auth_settings({})
    assert defaults.broker_url == DEFAULT_BROKER_URL
    assert defaults.token_file == DEFAULT_SERVICE_ACCOUNT_TOKEN_FILE
    assert defaults.fallback_token is None

    assert load_github_auth_settings({"CI_DASHBOARD_GITHUB_BROKER_URL": "  "}).broker_url == ""

    overridden = load_github_auth_settings(
        {
            "CI_DASHBOARD_GITHUB_BROKER_URL": "http://broker.example.svc",
            "CI_DASHBOARD_GITHUB_BROKER_TOKEN_FILE": "/tmp/sa/token",
            "CI_DASHBOARD_GITHUB_BROKER_TIMEOUT_SECONDS": "3",
            "CI_DASHBOARD_GITHUB_PAT_FALLBACK": "true",
            "GITHUB_TOKEN": "ghp_env",
        }
    )
    assert overridden.broker_url == "http://broker.example.svc"
    assert overridden.token_file == "/tmp/sa/token"
    assert overridden.timeout_seconds == 3
    assert overridden.fallback_token == "ghp_env"

    no_fallback = load_github_auth_settings({"GITHUB_TOKEN": "ghp_env"})
    assert no_fallback.fallback_token is None


def test_token_for_rejects_non_json_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=1))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    with pytest.raises(GitHubTokenError, match="non-JSON"):
        broker.token_for(request)


def test_token_for_rejects_non_object_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["ghs_x"])

    broker = _broker(handler, settings=_settings(tmp_path, max_attempts=1))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    with pytest.raises(GitHubTokenError, match="invalid response"):
        broker.token_for(request)


def test_token_without_expires_at_uses_default_ttl_and_is_cached(tmp_path: Path) -> None:
    issued = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        issued += 1
        return httpx.Response(200, json={"token": f"ghs_{issued}"})

    clock = _Clock()
    broker = _broker(handler, clock=clock, settings=_settings(tmp_path))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghs_1"
    clock.advance(60)
    assert broker.token_for(request) == "ghs_1"
    assert issued == 1


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-timestamp", 1730000000, None, "2026-10-27T09:00:00"],
)
def test_expires_at_is_parsed_defensively(tmp_path: Path, expires_at: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = {"token": "ghs_x"}
        if expires_at is not None:
            payload["expires_at"] = expires_at
        return httpx.Response(200, json=payload)

    clock = _Clock()
    broker = _broker(handler, clock=clock, settings=_settings(tmp_path))
    request = token_request_for_url("https://api.github.com/repos/pingcap/tidb/issues/1")
    assert request is not None

    assert broker.token_for(request) == "ghs_x"


def test_load_github_auth_settings_tolerates_invalid_values() -> None:
    settings = load_github_auth_settings(
        {
            "CI_DASHBOARD_GITHUB_BROKER_TIMEOUT_SECONDS": "not-a-number",
            "CI_DASHBOARD_GITHUB_PAT_FALLBACK": "maybe",
            "GITHUB_TOKEN": "ghp_env",
        }
    )
    assert settings.timeout_seconds == 10
    assert settings.fallback_token is None

    zero_timeout = load_github_auth_settings(
        {"CI_DASHBOARD_GITHUB_BROKER_TIMEOUT_SECONDS": "0"}
    )
    assert zero_timeout.timeout_seconds == 10
