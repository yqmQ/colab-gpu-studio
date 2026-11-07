"""Browser session restore, finite TTL, logout revocation, and CSRF boundaries.

No live credentials. Password and signing key exist only inside this test process.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.config import Settings, settings
from app.db import Store
from app.main import _login_limits, app
from app.security import read_session, sign_session
from fastapi.testclient import TestClient
from pydantic import ValidationError

PASSWORD = "correct-horse-battery"
SECRET = "test-session-private-signing-key-value"


@pytest.fixture
def client(tmp_path, monkeypatch):
    app.state.store = Store("sqlite:///" + str(tmp_path / "session.db"))
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "studio_password", PASSWORD)
    monkeypatch.setattr(settings, "session_secret", SECRET)
    monkeypatch.setattr(settings, "site_url", "http://localhost:8788")
    monkeypatch.setattr(settings, "session_ttl_seconds", 43200)
    monkeypatch.setattr(settings, "studio_api_token", "")
    _login_limits.clear()
    with TestClient(app) as test_client:
        yield test_client


def login(client, password=PASSWORD):
    return client.post("/api/auth/login", json={"password": password})


def cookie_parts(response):
    headers = response.headers.get_list("set-cookie")
    assert headers, "missing Set-Cookie"
    parts = [part.strip() for part in headers[0].split(";")]
    return parts[0], {part.split("=", 1)[0].casefold() for part in parts[1:]}


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_default_login_cookie_is_httponly_twelve_hours(client):
    response = login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 43200
    assert "data" not in body
    name, attrs = cookie_parts(response)
    assert name.startswith("studio_session=")
    assert attrs >= {"httponly", "path", "samesite", "max-age", "expires"}
    assert "secure" not in attrs
    header = response.headers.get_list("set-cookie")[0].casefold()
    assert "samesite=lax" in header
    assert "path=/" in header
    assert "max-age=43200" in header
    payload = read_session(body["access_token"])
    assert payload["exp"] - payload["iat"] == 43200
    assert payload["sv"] == 0


def test_https_cookie_is_secure(client, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://homelab.example")
    _name, attrs = cookie_parts(login(client))
    assert "secure" in attrs
    assert "httponly" in attrs


def test_session_ttl_env_is_bounded(monkeypatch):
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    assert Settings(_env_file=None).session_ttl_seconds == 43200
    monkeypatch.setenv("SESSION_TTL_SECONDS", "")
    assert Settings(_env_file=None).session_ttl_seconds == 43200
    monkeypatch.setenv("SESSION_TTL_SECONDS", "2592000")
    assert Settings(_env_file=None).session_ttl_seconds == 2592000
    monkeypatch.setenv("SESSION_TTL_SECONDS", "59")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("SESSION_TTL_SECONDS", "2592001")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_login_uses_configured_ttl_and_rejects_unbounded_override(client, monkeypatch):
    monkeypatch.setattr(settings, "session_ttl_seconds", 2592000)
    response = login(client)
    assert response.status_code == 200
    assert response.json()["expires_in"] == 2592000
    assert "max-age=2592000" in response.headers.get_list("set-cookie")[0].casefold()
    monkeypatch.setattr(settings, "session_ttl_seconds", 10)
    rejected = login(client)
    assert rejected.status_code == 500
    assert rejected.json()["code"] == "CONFIG_ERROR"


def test_refresh_new_tab_and_reopen_restore_bearer_without_extending_ttl(client):
    first = login(client)
    token = first.json()["access_token"]
    cookie = client.cookies.get("studio_session")
    assert cookie == token

    # Refresh: same tab still has the bearer and the cookie.
    refreshed = client.get("/api/me", headers=bearer(token))
    assert refreshed.status_code == 200
    assert refreshed.json()["id"] == "owner"

    # New tab: sessionStorage is empty, so only the cookie is presented.
    client.cookies.clear()
    client.cookies.set("studio_session", cookie)
    restored = client.get("/api/auth/session")
    assert restored.status_code == 200
    assert restored.json()["access_token"] == cookie
    assert 1 <= restored.json()["expires_in"] <= 43200
    assert "set-cookie" not in {name.casefold() for name in restored.headers}
    assert "access-control-allow-origin" not in {name.casefold() for name in restored.headers}
    created = client.post(
        "/api/gpu/projects",
        json={"title": "restored tab"},
        headers=bearer(restored.json()["access_token"]),
    )
    assert created.status_code == 201

    # Reopen: a fresh jar with only the surviving cookie, no Authorization header.
    client.cookies.clear()
    client.cookies.set("studio_session", cookie)
    reopened = client.get("/api/gpu/auth/google/config")
    assert reopened.status_code == 200
    again = client.get("/api/auth/session")
    assert again.json()["access_token"] == cookie


def test_cookie_cannot_authorize_writes_and_csrf_origin_is_rejected(client):
    token = login(client).json()["access_token"]
    cookie_only = client.post("/api/gpu/projects", json={"title": "cookie write"})
    assert cookie_only.status_code == 401
    oauth_cookie = client.post("/api/gpu/auth/google/config", json={"confirmed": False})
    assert oauth_cookie.status_code == 401

    oauth_bearer = client.post(
        "/api/gpu/auth/google/config",
        json={"confirmed": False},
        headers=bearer(token),
    )
    assert oauth_bearer.status_code == 403
    assert oauth_bearer.json()["code"] == "CONFIRMATION_REQUIRED"

    cross_site = client.post(
        "/api/gpu/projects",
        json={"title": "cross site"},
        headers={**bearer(token), "Origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["code"] == "CSRF_ORIGIN"

    # A bad bearer must not fall through to the cookie on writes.
    denied = client.post(
        "/api/gpu/projects",
        json={"title": "bad bearer"},
        headers=bearer("not-a-session-token"),
    )
    assert denied.status_code == 401


def test_expired_session_is_rejected_for_cookie_and_bearer(client):
    now = int(time.time())
    expired = sign_session(
        {"uid": "owner", "role": "admin", "iat": now - 120, "exp": now - 30, "sv": 0}
    )
    client.cookies.clear()
    client.cookies.set("studio_session", expired)
    assert client.get("/api/auth/session").status_code == 401
    assert client.get("/api/me", headers=bearer(expired)).status_code == 401
    assert client.post("/api/gpu/projects", json={"title": "expired"}, headers=bearer(expired)).status_code == 401


def test_legacy_token_without_generation_works_until_logout(client):
    now = int(time.time())
    legacy = sign_session({"uid": "owner", "role": "admin", "iat": now, "exp": now + 600})
    assert client.get("/api/me", headers=bearer(legacy)).status_code == 200
    fresh = login(client)
    assert client.post("/api/auth/logout", headers=bearer(fresh.json()["access_token"])).status_code == 200
    assert client.get("/api/me", headers=bearer(legacy)).status_code == 401
    assert client.get("/api/me", headers=bearer(fresh.json()["access_token"])).status_code == 401


def test_logout_revokes_tokens_clears_cookie_and_allows_a_new_login(client, tmp_path):
    first = login(client)
    token = first.json()["access_token"]
    # Anonymous logout must not revoke a bearer that was not presented.
    client.cookies.clear()
    anonymous = client.post("/api/auth/logout")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"logged_out": True}
    assert client.get("/api/me", headers=bearer(token)).status_code == 200

    client.cookies.set("studio_session", token)
    cross_site = client.post(
        "/api/auth/logout",
        headers={**bearer(token), "Origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403
    assert client.get("/api/me", headers=bearer(token)).status_code == 200

    signed_out = client.post("/api/auth/logout", headers=bearer(token))
    assert signed_out.status_code == 200
    cleared = signed_out.headers.get_list("set-cookie")[0].casefold()
    assert "studio_session=" in cleared
    assert "max-age=0" in cleared
    assert "httponly" in cleared
    assert read_session(token) is None
    assert client.get("/api/auth/session", headers=bearer(token)).status_code == 401
    assert client.get("/api/auth/session").status_code == 401
    assert client.post("/api/gpu/projects", json={"title": "after logout"}, headers=bearer(token)).status_code == 401
    assert (tmp_path / "session_epoch").read_text(encoding="utf-8").strip() == "1"
    assert token not in (tmp_path / "session_epoch").read_text(encoding="utf-8")

    again = login(client)
    assert again.status_code == 200
    assert client.get("/api/me", headers=bearer(again.json()["access_token"])).status_code == 200
    assert read_session(again.json()["access_token"])["sv"] == 1


def test_invalid_bearer_falls_back_to_cookie_only_for_session_restore(client):
    token = login(client).json()["access_token"]
    restored = client.get("/api/auth/session", headers=bearer("stale-browser-token"))
    assert restored.status_code == 200
    assert restored.json()["access_token"] == token
    assert "stale-browser-token" not in restored.text


def _no_token(response, secret):
    assert secret not in response.text
    assert "access_token" not in response.text


def test_cross_site_metadata_cannot_recover_a_bearer_or_logout(client):
    token = login(client).json()["access_token"]
    issued = read_session(token)
    same_origin = {
        "Origin": "http://localhost:8788",
        "Referer": "http://localhost:8788/",
        "Sec-Fetch-Site": "same-origin",
    }
    kept = client.get("/api/auth/session", headers=same_origin)
    assert kept.status_code == 200
    assert kept.json()["access_token"] == token
    assert read_session(kept.json()["access_token"])["exp"] == issued["exp"]
    assert "set-cookie" not in {name.casefold() for name in kept.headers}
    assert kept.headers["cache-control"] == "private, no-store"
    assert "access-control-allow-origin" not in {name.casefold() for name in kept.headers}
    assert "access-control-allow-credentials" not in {name.casefold() for name in kept.headers}

    attacks = [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:8788.evil.com"},
        {"Referer": "https://evil.example/phish"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"},
    ]
    for headers in attacks:
        denied = client.get("/api/auth/session", headers=headers)
        assert denied.status_code == 403, headers
        assert denied.json()["code"] == "CSRF_ORIGIN"
        _no_token(denied, token)
        logged_out = client.post("/api/auth/logout", headers={**headers, **bearer(token)})
        assert logged_out.status_code == 403, headers
        _no_token(logged_out, token)
        assert client.get("/api/me", headers=bearer(token)).status_code == 200


def test_non_browser_cookie_holder_still_receives_the_bearer(client):
    """Residual: no Origin, Referer, or Sec-Fetch-Site. The cookie value is already the bearer."""
    token = login(client).json()["access_token"]
    client.cookies.clear()
    client.cookies.set("studio_session", token)
    restored = client.get("/api/auth/session")
    assert restored.status_code == 200
    assert restored.json()["access_token"] == token


def test_every_write_rejects_cookie_only_and_cross_site_callers(client):
    token = login(client).json()["access_token"]
    before = client.get("/api/gpu/projects", headers=bearer(token)).json()["data"]
    writes = []
    for path, operations in client.app.openapi()["paths"].items():
        for method in operations:
            if method.lower() not in {"post", "put", "patch", "delete"}:
                continue
            if path == "/api/auth/login":
                continue
            writes.append((method.upper(), re.sub(r"\{[^}]+\}", "missing", path)))
    assert writes, "expected write operations in the OpenAPI document"
    for method, path in writes:
        evil = client.request(method, path, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}, json={})
        assert evil.status_code == 403, (method, path, evil.status_code, evil.text[:200])
        assert evil.json()["code"] == "CSRF_ORIGIN"
        _no_token(evil, token)
        if path == "/api/auth/logout":
            continue
        cookie_only = client.request(method, path, json={})
        assert cookie_only.status_code == 401, (method, path, cookie_only.status_code, cookie_only.text[:200])
        _no_token(cookie_only, token)
    assert client.get("/api/me", headers=bearer(token)).status_code == 200
    after = client.get("/api/gpu/projects", headers=bearer(token)).json()["data"]
    assert after == before


def test_https_logout_clears_the_secure_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "site_url", "https://studio.example")
    response = login(client)
    _name, attrs = cookie_parts(response)
    assert {"secure", "httponly", "samesite", "path", "max-age", "expires"} <= attrs
    token = response.json()["access_token"]
    signed_out = client.post(
        "/api/auth/logout",
        headers={**bearer(token), "Origin": "https://studio.example", "Sec-Fetch-Site": "same-origin"},
    )
    assert signed_out.status_code == 200
    folded = signed_out.headers.get_list("set-cookie")[0].casefold()
    assert "max-age=0" in folded
    assert "httponly" in folded
    assert "secure" in folded
    assert "samesite=lax" in folded
    assert "path=/" in folded
    assert client.get("/api/auth/session", headers=bearer(token)).status_code == 401
    assert client.get("/api/me", headers=bearer(token)).status_code == 401


def test_logout_revocation_survives_a_new_process(client, tmp_path):
    token = login(client).json()["access_token"]
    assert client.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert _session_in_new_process(tmp_path, token) == "REVOKED"
    fresh = login(client).json()["access_token"]
    assert _session_in_new_process(tmp_path, fresh) == "VALID"
    assert read_session(token) is None


def _session_in_new_process(data_dir, token):
    env = os.environ.copy()
    env["ENVIRONMENT"] = "offline-test"
    env["DATA_DIR"] = str(data_dir)
    env["SESSION_SECRET"] = SECRET
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "api")
    env.pop("STUDIO_OFFLINE_TEST", None)
    code = (
        "import sys\n"
        "from app.security import read_session\n"
        "token = sys.stdin.read()\n"
        "sys.stdout.write('REVOKED\\n' if read_session(token) is None else 'VALID\\n')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=token,
        text=True,
        capture_output=True,
        env=env,
        cwd=data_dir,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_mismatched_api_token_length_does_not_crash_writes(client, monkeypatch):
    monkeypatch.setattr(settings, "studio_api_token", "static-api-token-value-0123456789")
    response = client.post(
        "/api/gpu/projects",
        json={"title": "short token"},
        headers=bearer("short"),
    )
    assert response.status_code == 401
    assert "static-api-token" not in response.text


def test_static_api_token_is_not_a_browser_session(client, monkeypatch):
    api_token = "static-api-token-value-0123456789"
    monkeypatch.setattr(settings, "studio_api_token", api_token)
    token = login(client).json()["access_token"]
    hidden = client.get("/api/auth/session", headers=bearer(api_token))
    assert hidden.status_code == 401
    assert api_token not in hidden.text
    assert token not in hidden.text
    assert client.get("/api/me", headers=bearer(api_token)).status_code == 200
