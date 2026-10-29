from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from ci_dashboard.api import auth
from ci_dashboard.api.dependencies import get_engine
from ci_dashboard.api.main import create_app

CLIENT_ID = "dashboard-test.apps.googleusercontent.com"


@pytest.fixture(scope="module")
def signing_keys():
    def make_key(key_id):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, key_id)])
        public = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
            .decode()
        )
        return crypt.RSASigner.from_string(private), public

    signer, public = make_key("google-key")
    other_signer, other_public = make_key("google-key")
    return signer, public, other_signer, other_public


@pytest.fixture()
def secured_app(monkeypatch, local_api_auth, signing_keys):
    monkeypatch.delenv("CI_DASHBOARD_AUTH_DISABLED")
    monkeypatch.setenv("CI_DASHBOARD_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", "pingcap.com")
    auth.get_identity_verifier.cache_clear()
    requests = []

    def certificates(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(
            200,
            json={"google-key": signing_keys[1]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(auth.httpx, "get", certificates)
    app = create_app()

    @app.get("/api/auth-check")
    def auth_check():
        return {"ok": True}

    app.router.routes.insert(0, app.router.routes.pop())  # Before the frontend catch-all.
    app.state.certificate_requests = requests
    return app


@pytest.fixture()
def token(signing_keys):
    def encode(**overrides):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "company-user",
            "email": "employee@pingcap.com",
            "email_verified": True,
            "hd": "pingcap.com",
            "iat": int(time.time()) - 10,
            "exp": int(time.time()) + 3600,
        }
        claims.update(overrides)
        return jwt.encode(signing_keys[0], claims, header={"kid": "google-key"}).decode()

    return encode


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/pages/cost",
        "/api/v1/pages/cost-sources",
        "/api/v1/pages/cost-filter-values",
        "/api/v1/pages/cost-share?dimension=owner",
        "/api/v1/pages/cost-unmatched-resources?page_size=1",
        "/api/v1/pages/weekly-cost",
        "/api/v1/status/freshness",
        "/api/v1/filters/repos",
        "/api/v1/builds/trend",
        "/api/v1/pages/runtime-error-builds?limit=-1",
        "/",
        "/assets/app.js",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/unknown",
    ],
)
def test_missing_identity_rejected_before_queries_or_validation(secured_app, path):
    def forbidden_engine():
        pytest.fail("unauthorized request reached the database")

    secured_app.dependency_overrides[get_engine] = forbidden_engine
    with TestClient(secured_app) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert secured_app.state.certificate_requests == []


def test_plain_headers_bearer_and_cookies_are_not_identity(secured_app, token):
    with TestClient(secured_app) as client:
        response = client.get(
            "/api/auth-check",
            headers={
                "X-Forwarded-Email": "employee@pingcap.com",
                "X-Envoy-Internal": "true",
                "Authorization": f"Bearer {token()}",
                "Cookie": f"IdToken={token()}",
            },
        )
    assert response.status_code == 401
    assert secured_app.state.certificate_requests == []


@pytest.mark.parametrize("issuer", ["accounts.google.com", "https://accounts.google.com"])
def test_signed_company_identity_is_accepted_and_certificates_cached(secured_app, token, issuer):
    with TestClient(secured_app) as client:
        for _ in range(2):
            response = client.get(
                "/api/auth-check",
                headers={
                    auth.ID_TOKEN_HEADER: token(iss=issuer),
                },
            )
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert response.headers["cache-control"] == "private, no-store"
    assert secured_app.state.certificate_requests == [
        (auth.GOOGLE_CERTS_URL, {"timeout": 5.0, "follow_redirects": False}),
    ]


def test_authenticated_cost_sources_route(secured_app, token, sqlite_engine):
    secured_app.dependency_overrides[get_engine] = lambda: sqlite_engine
    with TestClient(secured_app) as client:
        response = client.get(
            "/api/v1/pages/cost-sources",
            headers={"X-CI-Dashboard-ID-Token": token()},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-app"},
        {"aud": None},
        {"aud": [CLIENT_ID, "another-app"]},
        {"iss": "https://attacker.invalid"},
        {"iss": None},
        {"exp": 1},
        {"exp": None},
        {"exp": "not-a-date"},
        {"exp": float("nan")},
        {"iat": int(time.time()) + 7200},
        {"iat": None},
        {"iat": True},
        {"sub": ""},
        {"sub": None},
        {"sub": 42},
        {"email": ""},
        {"email": None},
        {"azp": "another-app"},
    ],
)
def test_invalid_signed_claims_are_rejected(secured_app, token, claims):
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: token(**claims)})
    assert response.status_code == 401
    assert response.json() == {"detail": "valid company identity required"}


@pytest.mark.parametrize(
    "claim,offset,status",
    [("iat", 20, 200), ("iat", 60, 401), ("exp", -20, 200), ("exp", -60, 401)],
)
def test_token_time_checks_allow_only_small_clock_skew(secured_app, token, claim, offset, status):
    now = int(time.time())
    claims = {"iat": now - 3600, "exp": now + 3600, claim: now + offset}
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: token(**claims)})
    assert response.status_code == status


@pytest.mark.parametrize(
    "claims",
    [
        {"hd": None},
        {"hd": "external.example"},
        {"hd": "pingcap.com.attacker.example"},
        {"hd": ["pingcap.com"]},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email_verified": 1},
        {"email_verified": None},
        # A signed email suffix without Google's signed Workspace claim is insufficient.
        {"email": "employee@pingcap.com", "hd": None},
    ],
)
def test_noncompany_or_unverified_identity_is_forbidden(secured_app, token, claims):
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: token(**claims)})
    assert response.status_code == 403


def test_only_configured_hosted_domains_are_allowed(secured_app, token, monkeypatch):
    monkeypatch.setenv("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", " pingcap.com, approved.example ")
    with TestClient(secured_app) as client:
        response = client.get(
            "/api/auth-check",
            headers={
                auth.ID_TOKEN_HEADER: token(hd="approved.example"),
            },
        )
    assert response.status_code == 200


def test_forged_signature_is_rejected(secured_app, token, signing_keys):
    claims = jwt.decode(token(), verify=False)
    forged = jwt.encode(signing_keys[2], claims, header={"kid": "google-key"}).decode()
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: forged})
    assert response.status_code == 401


@pytest.mark.parametrize("value", ["", "not-a-token", "a.b.c", "x" * (auth.MAX_TOKEN_BYTES + 1)])
def test_malformed_or_oversized_token_does_not_fetch_keys(secured_app, value):
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: value})
    assert response.status_code == 401
    assert secured_app.state.certificate_requests == []


@pytest.mark.parametrize("header", [{"alg": "none"}, {"alg": "HS256"}, {"kid": ""}])
def test_unsupported_algorithm_or_missing_key_id(secured_app, token, signing_keys, header):
    encoded = jwt.encode(
        signing_keys[0],
        jwt.decode(token(), verify=False),
        header={"kid": "google-key", **header},
    ).decode()
    with TestClient(secured_app) as client:
        response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: encoded})
    assert response.status_code == 401
    assert secured_app.state.certificate_requests == []


def test_duplicate_identity_headers_are_rejected(secured_app, token):
    with TestClient(secured_app) as client:
        response = client.get(
            "/api/auth-check",
            headers=[
                (auth.ID_TOKEN_HEADER, token()),
                (auth.ID_TOKEN_HEADER, token()),
            ],
        )
    assert response.status_code == 401
    assert secured_app.state.certificate_requests == []


def test_unknown_key_ids_only_refresh_once_per_cooldown(
    secured_app, token, signing_keys, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    with TestClient(secured_app) as client:
        for elapsed, expected_calls in (
            (0, 1),
            (auth.CERT_RETRY_SECONDS, 2),
            (auth.CERT_RETRY_SECONDS - 1, 2),
            (1, 3),
        ):
            clock[0] += elapsed
            for key_id in ("unknown-1", "unknown-2", "unknown-3"):
                encoded = jwt.encode(
                    signing_keys[0],
                    jwt.decode(token(), verify=False),
                    header={"kid": key_id},
                ).decode()
                response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: encoded})
                assert response.status_code == 401
            assert len(secured_app.state.certificate_requests) == expected_calls


def test_rotated_key_is_accepted_after_refresh_cooldown(
    secured_app, token, signing_keys, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    encoded = token()
    rotated = jwt.encode(
        signing_keys[2], jwt.decode(encoded, verify=False), header={"kid": "rotated-key"}
    ).decode()
    verifier = auth.get_identity_verifier()
    verifier.verify(encoded)
    calls = []

    def certificates(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            json={"google-key": signing_keys[1], "rotated-key": signing_keys[3]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(auth.httpx, "get", certificates)
    with pytest.raises(HTTPException) as exc:
        verifier.verify(rotated)
    assert exc.value.status_code == 401
    assert calls == []
    clock[0] += auth.CERT_RETRY_SECONDS
    verifier.verify(rotated)
    verifier.verify(encoded)
    verifier.verify(rotated)
    assert calls == [auth.GOOGLE_CERTS_URL]
    assert len(secured_app.state.certificate_requests) == 1


def test_failed_rotation_refresh_keeps_valid_keys_and_respects_cooldown(
    secured_app, token, signing_keys, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    encoded = token()
    unknown = jwt.encode(
        signing_keys[0], jwt.decode(encoded, verify=False), header={"kid": "unknown"}
    ).decode()
    verifier = auth.get_identity_verifier()
    verifier.verify(encoded)
    calls = []

    def unavailable(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(auth.httpx, "get", unavailable)
    clock[0] += auth.CERT_RETRY_SECONDS
    for status in (503, 401, 401):
        with pytest.raises(HTTPException) as exc:
            verifier.verify(unknown)
        assert exc.value.status_code == status
        verifier.verify(encoded)
    assert len(calls) == 1
    clock[0] += auth.CERT_RETRY_SECONDS
    with pytest.raises(HTTPException) as exc:
        verifier.verify(unknown)
    assert exc.value.status_code == 503
    assert len(calls) == 2


def test_certificates_refresh_after_cache_expiry(secured_app, token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    verifier = auth.get_identity_verifier()
    verifier.verify(token())
    clock[0] += auth.CERT_CACHE_SECONDS + 1
    verifier.verify(token())
    assert len(secured_app.state.certificate_requests) == 2


@pytest.mark.parametrize(
    "max_age,lifetime", [("0", 60), ("1", 60), ("60", 60), ("7200", auth.CERT_CACHE_SECONDS)]
)
def test_certificate_cache_respects_provider_lifetime(
    secured_app,
    token,
    monkeypatch,
    signing_keys,
    max_age,
    lifetime,
):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])

    def certificates(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            json={"google-key": signing_keys[1]},
            headers={"Cache-Control": f"public, max-age={max_age}, must-revalidate"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(auth.httpx, "get", certificates)
    verifier = auth.get_identity_verifier()
    verifier.verify(token())
    clock[0] += lifetime - 1
    verifier.verify(token())
    assert len(calls) == 1
    clock[0] += 2
    verifier.verify(token())
    assert len(calls) == 2


def test_expired_cached_keys_are_not_used_during_outage(secured_app, token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    verifier = auth.get_identity_verifier()
    verifier.verify(token())
    clock[0] += auth.CERT_CACHE_SECONDS + 1

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(auth.httpx, "get", unavailable)
    with pytest.raises(Exception) as exc:
        verifier.verify(token())
    assert exc.value.status_code == 503


def test_concurrent_verification_fetches_certificates_once(secured_app, token):
    verifier = auth.get_identity_verifier()
    encoded = token()
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(verifier.verify, [encoded] * 16))
    assert len(secured_app.state.certificate_requests) == 1


def test_slow_certificate_fetch_does_not_block_other_verifications(secured_app, token, monkeypatch):
    fetching = Event()
    release = Event()
    original = auth.httpx.get

    def certificates(url, **kwargs):
        fetching.set()
        assert release.wait(timeout=5)
        return original(url, **kwargs)

    monkeypatch.setattr(auth.httpx, "get", certificates)
    verifier = auth.get_identity_verifier()
    encoded = token()
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(verifier.verify, encoded)
        try:
            assert fetching.wait(timeout=1)
            follower = executor.submit(verifier.verify, encoded)
            with pytest.raises(HTTPException) as exc:
                follower.result(timeout=1)
            assert exc.value.status_code == 503
            assert not leader.done()
        finally:
            release.set()
        leader.result(timeout=1)
    verifier.verify(encoded)
    assert len(secured_app.state.certificate_requests) == 1


def test_unknown_key_refresh_does_not_block_valid_cached_identities(
    secured_app,
    token,
    signing_keys,
    monkeypatch,
):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    verifier = auth.get_identity_verifier()
    encoded = token()
    verifier.verify(encoded)
    clock[0] += auth.CERT_RETRY_SECONDS
    unknown = jwt.encode(
        signing_keys[0],
        jwt.decode(encoded, verify=False),
        header={"kid": "unknown"},
    ).decode()
    fetching, release = Event(), Event()
    original = auth.httpx.get

    def certificates(url, **kwargs):
        fetching.set()
        assert release.wait(timeout=5)
        return original(url, **kwargs)

    monkeypatch.setattr(auth.httpx, "get", certificates)
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh = executor.submit(verifier.verify, unknown)
        try:
            assert fetching.wait(timeout=1)
            executor.submit(verifier.verify, encoded).result(timeout=1)
            assert not refresh.done()
        finally:
            release.set()
        with pytest.raises(HTTPException) as exc:
            refresh.result(timeout=1)
        assert exc.value.status_code == 401


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "schema", "empty", "pem"])
def test_key_fetch_failure_is_closed_and_rate_limited(
    secured_app, token, monkeypatch, failure, caplog
):
    calls = []

    def unavailable(url, **kwargs):
        calls.append(url)
        if failure == "timeout":
            raise httpx.ConnectTimeout("private diagnostic must not leak")
        response = httpx.Response(
            503 if failure == "http" else 200,
            content=b"not-json" if failure == "json" else None,
            json=(
                {"kid": []}
                if failure == "schema"
                else {"google-key": "not-a-certificate"}
                if failure == "pem"
                else {}
            ),
            request=httpx.Request("GET", url),
        )
        return response

    monkeypatch.setattr(auth.httpx, "get", unavailable)
    with TestClient(secured_app) as client:
        for _ in range(2):
            response = client.get("/api/auth-check", headers={auth.ID_TOKEN_HEADER: token()})
            assert response.status_code == 503
            assert response.json() == {"detail": "authentication unavailable"}
    assert len(calls) == 1
    assert "private diagnostic" not in caplog.text


def test_failed_key_fetch_is_retried_after_cooldown(secured_app, token, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    original = auth.httpx.get
    monkeypatch.setattr(
        auth.httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )
    verifier = auth.get_identity_verifier()
    with pytest.raises(Exception) as exc:
        verifier.verify(token())
    assert exc.value.status_code == 503
    monkeypatch.setattr(auth.httpx, "get", original)
    clock[0] += auth.CERT_RETRY_SECONDS + 1
    verifier.verify(token())


@pytest.mark.parametrize(
    "variable,value",
    [
        ("CI_DASHBOARD_OIDC_CLIENT_ID", ""),
        ("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", ""),
        ("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", "*"),
        ("CI_DASHBOARD_AUTH_DISABLED", "yes"),
    ],
)
def test_bad_configuration_is_closed(secured_app, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with TestClient(secured_app) as client:
        assert client.get("/api/auth-check").status_code == 503
        assert client.get("/readyz").status_code == 503
        assert client.get("/livez").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert secured_app.state.certificate_requests == []


@pytest.mark.parametrize(
    "variable,value",
    [
        ("CI_DASHBOARD_OIDC_CLIENT_ID", ""),
        ("CI_DASHBOARD_OIDC_HOSTED_DOMAINS", "*.private-company-domain.example"),
        ("CI_DASHBOARD_AUTH_DISABLED", "private-invalid-value"),
    ],
)
def test_configuration_failure_logs_variable_name_only_once(
    secured_app,
    monkeypatch,
    caplog,
    variable,
    value,
):
    monkeypatch.setenv(variable, value)
    with caplog.at_level("ERROR", logger="ci_dashboard.api.auth"):
        with TestClient(secured_app) as client:
            for path in ("/api/auth-check", "/api/auth-check", "/readyz"):
                assert client.get(path).status_code == 503
    records = [record for record in caplog.records if record.name == "ci_dashboard.api.auth"]
    assert len(records) == 1
    assert variable in records[0].getMessage()
    assert "private-" not in records[0].getMessage()


def test_unconfigured_default_is_not_anonymous(monkeypatch, local_api_auth):
    for variable in (
        "CI_DASHBOARD_AUTH_DISABLED",
        "CI_DASHBOARD_OIDC_CLIENT_ID",
        "CI_DASHBOARD_OIDC_HOSTED_DOMAINS",
    ):
        monkeypatch.delenv(variable, raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/pages/cost-sources").status_code == 503


def test_explicit_local_development_opt_out(monkeypatch, local_api_auth):
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_probes_are_available_without_identity(secured_app, sqlite_engine, monkeypatch):
    monkeypatch.setattr("ci_dashboard.api.main.get_engine", lambda: sqlite_engine)
    with TestClient(secured_app) as client:
        for path in auth.PROBE_PATHS:
            assert client.get(path).status_code == 200
    assert secured_app.state.certificate_requests == []
