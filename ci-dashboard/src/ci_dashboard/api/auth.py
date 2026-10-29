"""Verify company identities even when requests bypass the OIDC gateway."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from functools import lru_cache

import httpx
from fastapi import HTTPException
from google.auth import crypt, jwt
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOG = logging.getLogger(__name__)

ID_TOKEN_HEADER = "X-CI-Dashboard-ID-Token"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
PROBE_PATHS = frozenset({"/healthz", "/livez", "/readyz"})
MAX_TOKEN_BYTES = 16384
CERT_CACHE_SECONDS = 3600
CERT_RETRY_SECONDS = 10
CERT_WAIT_SECONDS = 0.1
CLOCK_SKEW_SECONDS = 30


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="authentication unavailable")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="valid company identity required")


class CompanyIdentityVerifier:
    def __init__(self, client_id: str, hosted_domains: frozenset[str]) -> None:
        self.client_id = client_id
        self.hosted_domains = hosted_domains
        self._cache: tuple[float, dict[str, str]] = (0.0, {})
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def _get_certs(self, key_id: str) -> dict[str, str]:
        # A rotation probe must not disrupt identities whose cached keys are still valid.
        expires_at, certs = self._cache
        if time.monotonic() < expires_at and key_id in certs:
            return certs
        # Share fetches, but do not tie up worker threads behind a slow download.
        if not self._lock.acquire(timeout=CERT_WAIT_SECONDS):
            raise _unavailable()
        try:
            now = time.monotonic()
            expires_at, certs = self._cache
            if now < expires_at and (key_id in certs or now < self._retry_at):
                return certs
            if now < self._retry_at:
                raise _unavailable()
            try:
                response = httpx.get(GOOGLE_CERTS_URL, timeout=5.0, follow_redirects=False)
                response.raise_for_status()
                certs = response.json()
                max_age = re.search(
                    r"(?:^|,)\s*max-age=(\d+)", response.headers.get("Cache-Control", "")
                )
                lifetime = (
                    min(CERT_CACHE_SECONDS, max(60, int(max_age[1])))
                    if max_age
                    else CERT_CACHE_SECONDS
                )
                if (
                    not isinstance(certs, dict)
                    or not certs
                    or not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in certs.items()
                    )
                ):
                    raise ValueError("invalid certificate response")
                for certificate in certs.values():
                    crypt.RSAVerifier.from_string(certificate)
            except (httpx.HTTPError, ValueError, TypeError):
                self._retry_at = time.monotonic() + CERT_RETRY_SECONDS
                raise _unavailable() from None
            now = time.monotonic()
            self._cache = (now + lifetime, certs)
            # Rate-limit unknown-key refreshes after every download, including cold starts.
            self._retry_at = now + CERT_RETRY_SECONDS
            return certs
        finally:
            self._lock.release()

    def verify(self, token: str) -> None:
        if not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise _unauthorized()
        try:
            header = jwt.decode_header(token)
            if (
                not isinstance(header, dict)
                or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
            ):
                raise ValueError("invalid token header")
        except (ValueError, TypeError):
            raise _unauthorized() from None

        certs = self._get_certs(header["kid"])
        try:
            claims = jwt.decode(
                token,
                certs=certs,
                audience=self.client_id,
                clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
            )
            if (
                claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                or type(claims.get("iat")) is not int
                or type(claims.get("exp")) is not int
                or claims["exp"] <= claims["iat"]
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"]
                or not isinstance(claims.get("email"), str)
                or not claims["email"]
                or claims.get("azp", self.client_id) != self.client_id
            ):
                raise ValueError("invalid token claims")
        except (ValueError, TypeError, KeyError, AttributeError):
            # Do not expose library errors, claims or credentials in responses/logs.
            raise _unauthorized() from None
        if (
            claims.get("email_verified") is not True
            or not isinstance(claims.get("hd"), str)
            or claims["hd"] not in self.hosted_domains
        ):
            raise HTTPException(status_code=403, detail="company identity required")


@lru_cache(maxsize=3)
def _warn_configuration(variable: str) -> None:
    LOG.error("Missing or invalid authentication configuration: %s", variable)


@lru_cache(maxsize=1)
def get_identity_verifier() -> CompanyIdentityVerifier | None:
    disabled = os.environ.get("CI_DASHBOARD_AUTH_DISABLED", "false").strip().lower()
    if disabled == "true":
        return None
    if disabled != "false":
        _warn_configuration("CI_DASHBOARD_AUTH_DISABLED")
        raise _unavailable()
    client_id = os.environ.get("CI_DASHBOARD_OIDC_CLIENT_ID", "").strip()
    domains = frozenset(
        domain.strip().lower()
        for domain in os.environ.get("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", "").split(",")
        if domain.strip()
    )
    if not client_id:
        _warn_configuration("CI_DASHBOARD_OIDC_CLIENT_ID")
        raise _unavailable()
    if not domains or any("*" in domain for domain in domains):
        _warn_configuration("CI_DASHBOARD_OIDC_HOSTED_DOMAINS")
        raise _unavailable()
    return CompanyIdentityVerifier(client_id, domains)


class CompanyIdentityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["path"] in PROBE_PATHS and scope["method"] in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return
        try:
            verifier = get_identity_verifier()
            if verifier is not None:
                tokens = Headers(scope=scope).getlist(ID_TOKEN_HEADER)
                if len(tokens) != 1:
                    raise _unauthorized()
                await run_in_threadpool(verifier.verify, tokens[0])
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        async def authenticated_send(message):
            if (
                verifier is not None
                and scope["path"].startswith("/api/")
                and message["type"] == "http.response.start"
            ):
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        await self.app(scope, receive, authenticated_send)
