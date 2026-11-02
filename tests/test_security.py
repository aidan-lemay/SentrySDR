"""Internet-facing behaviour: who can get in, and what they can't do."""
from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentrysdr.config import Config, ConfigError, validate
from sentrysdr.context import Context
from sentrysdr.models import User
from sentrysdr.security import client_ip
from sentrysdr.web import create_app

from .conftest import ADMIN_EMAIL, HEADERS, PASSWORD, add_user, client_for, make_config


# --- configuration refuses unsafe exposure ---------------------------------
def test_public_bind_refused_by_default():
    with pytest.raises(ConfigError, match="not loopback"):
        validate(Config(bind_host="0.0.0.0"))


def test_plain_http_public_url_refused():
    with pytest.raises(ConfigError, match="https"):
        validate(Config(public_url="http://sentry.example.com"))


@pytest.mark.parametrize("url", ["http://100.100.1.2:8642", "http://master.tail1234.ts.net", "https://sentry.example.com"])
def test_slave_accepts_secure_or_private_master(url):
    validate(Config(role="slave", master_url=url))


def test_slave_refuses_plain_http_over_public_internet():
    with pytest.raises(ConfigError, match="plain http"):
        validate(Config(role="slave", master_url="http://sentry.example.com"))


def test_forwarding_header_needs_trusted_proxy():
    with pytest.raises(ConfigError, match="trusted_proxies"):
        validate(Config(client_ip_header="CF-Connecting-IP"))


# --- client address cannot be spoofed --------------------------------------
def test_spoofed_header_ignored_from_untrusted_peer():
    cfg = Config(trusted_proxies=["127.0.0.1/32"], client_ip_header="CF-Connecting-IP")
    assert client_ip(cfg, "203.0.113.9", {"CF-Connecting-IP": "1.1.1.1"}) == "203.0.113.9"


def test_header_used_from_trusted_tunnel():
    cfg = Config(trusted_proxies=["127.0.0.1/32"], client_ip_header="CF-Connecting-IP")
    assert client_ip(cfg, "127.0.0.1", {"CF-Connecting-IP": "198.51.100.7"}) == "198.51.100.7"


def test_x_forwarded_for_takes_rightmost_untrusted():
    cfg = Config(trusted_proxies=["127.0.0.1/32"], client_ip_header="X-Forwarded-For")
    headers = {"X-Forwarded-For": "6.6.6.6, 198.51.100.7"}  # the client forged the left entry
    assert client_ip(cfg, "127.0.0.1", headers) == "198.51.100.7"


# --- requests refused before reaching a route ------------------------------
def test_unknown_host_refused(app):
    client = TestClient(app, base_url="https://evil.example.net")
    assert client.get("/api/health").status_code == 400


def test_cross_origin_post_refused(app):
    client = TestClient(app, base_url="https://sentry.example.com")
    response = client.post("/api/auth/logout", headers={"Origin": "https://evil.example.net"})
    assert response.status_code == 403


def test_post_without_origin_or_header_refused(app):
    client = TestClient(app, base_url="https://sentry.example.com")
    assert client.post("/api/auth/logout").status_code == 403


def test_same_origin_post_allowed(app):
    client = TestClient(app, base_url="https://sentry.example.com")
    response = client.post("/api/auth/logout", headers={"Origin": "https://sentry.example.com"})
    assert response.status_code == 200


def test_security_headers_present(app):
    headers = TestClient(app, base_url="https://sentry.example.com").get("/api/health").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["strict-transport-security"]
    assert "server" not in headers


def test_looser_tar1090_policy_stays_on_its_own_path(app):
    client = TestClient(app, base_url="https://sentry.example.com")
    tar1090 = client.get("/api/tar1090/index.html").headers["content-security-policy"]
    assert "worker-src 'self' blob:" in tar1090 and "'unsafe-inline' 'unsafe-eval'" in tar1090
    main = client.get("/api/health").headers["content-security-policy"]
    assert "blob:" not in main and "unsafe-eval" not in main and "script-src 'self';" in main


def test_api_docs_not_exposed(app):
    client = TestClient(app, base_url="https://sentry.example.com")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert "openapi" not in client.get(path).text.lower()


def test_oversized_body_refused(app):
    client = client_for(app)
    response = client.post("/api/auth/login", content=b"x" * 2_000_000, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_unauthenticated_call_upload_refused_before_parsing(app):
    client = client_for(app)
    response = client.post("/api/nodes/agent/calls", files={"audio": ("a.mp3", b"x" * 100)}, data={"meta": "{}"})
    assert response.status_code == 401


def test_static_path_traversal_blocked(app, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>")
    (tmp_path / "secret.txt").write_text("top secret")
    app.state.web_dist = dist
    client = client_for(app)
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        assert "top secret" not in client.get(path).text


# --- every data route needs a session --------------------------------------
@pytest.mark.parametrize("path", [
    "/api/me", "/api/sources", "/api/targets", "/api/recorders", "/api/recordings", "/api/maps/adsb",
    "/api/live/1", "/api/admin/users", "/api/admin/audit", "/api/admin/settings", "/api/nodes",
    "/api/recordings/x/audio", "/api/tar1090/index.html",
])
def test_routes_require_sign_in(app, path):
    assert client_for(app).get(path).status_code == 401


def test_every_route_is_guarded_or_explicitly_public(app):
    """A new route that forgets its auth dependency fails here."""
    public = {"/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/health",
              "/api/nodes/agent/enroll", "/api/nodes/agent/calls", "/api/nodes/agent/ws",
              "/api/{_rest:path}", "/{path:path}"}
    client = client_for(app)
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api") or path in public or not getattr(route, "methods", None):
            continue
        url = path.replace("{source_id}", "1").replace("{user_id}", "1").replace("{path:path}", "x")
        for name in ("{target_id}", "{recorder_id}", "{recording_id}", "{node_id}", "{ban_id}", "{kind}", "{name}"):
            url = url.replace(name, "1")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            assert client.request(method, url, json={}).status_code == 401, f"{method} {path} is open"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/admin/users"), ("get", "/api/admin/audit"), ("get", "/api/admin/settings"),
    ("get", "/api/nodes"), ("post", "/api/admin/bans"), ("post", "/api/sources"),
])
def test_admin_routes_refuse_other_roles(listener, operator, method, path):
    assert getattr(listener, method)(path).status_code == 403 if method == "get" else True
    kwargs = {} if method == "get" else {"json": {"kind": "ip", "value": "1.2.3.4", "serial": "SER1", "name": "x"}}
    assert getattr(operator, method)(path, **kwargs).status_code == 403


# --- sign-up and sign-in ---------------------------------------------------
def signup(client, email="new@example.com", password=PASSWORD):
    return client.post("/api/auth/signup", json={"email": email, "name": "New", "password": password})


def test_signup_grants_no_access_until_approved(app, admin, ctx):
    client = client_for(app)
    assert signup(client).status_code == 202
    response = client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert response.status_code == 403 and "approval" in response.text

    pending = admin.get("/api/admin/users", params={"status": "pending"}).json()
    assert [u["email"] for u in pending] == ["new@example.com"]
    admin.patch(f"/api/admin/users/{pending[0]['id']}", json={"status": "active"})
    assert client.post("/api/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 200
    assert client.get("/api/me").json()["role"] == "listener"


def test_signup_cannot_choose_its_own_role(app, ctx):
    client = client_for(app)
    client.post("/api/auth/signup", json={"email": "x@example.com", "name": "X", "password": PASSWORD, "role": "admin", "status": "active"})
    with ctx.db() as session:
        user = session.query(User).filter_by(email="x@example.com").one()
        assert (user.role, user.status) == ("listener", "pending")


def test_signup_reply_does_not_reveal_existing_accounts(app, admin):
    client = client_for(app)
    assert signup(client, ADMIN_EMAIL).json() == signup(client, "fresh@example.com").json()


def test_signup_rate_limited(app):
    client = client_for(app)
    codes = [signup(client, f"u{i}@example.com").status_code for i in range(7)]
    assert codes[:5] == [202] * 5 and codes[5:] == [429, 429]


def test_short_password_refused(app):
    assert signup(client_for(app), password="short").status_code == 422


def test_login_error_identical_for_unknown_and_wrong_password(app, admin):
    client = client_for(app)
    wrong = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "nope-nope-nope"})
    unknown = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())


def test_login_locks_after_repeated_failures(app, admin):
    client = client_for(app)
    bad = {"email": ADMIN_EMAIL, "password": "wrong-wrong-wrong"}
    codes = [client.post("/api/auth/login", json=bad).status_code for _ in range(9)]
    assert codes[:8] == [401] * 8 and codes[8] == 429
    good = {"email": ADMIN_EMAIL, "password": PASSWORD}
    assert client.post("/api/auth/login", json=good).status_code == 429


def test_passwords_stored_as_argon2id(ctx, admin):
    with ctx.db() as session:
        assert session.query(User).filter_by(email=ADMIN_EMAIL).one().password_hash.startswith("$argon2id$")


def test_session_cookie_flags(ctx, app):
    add_user(ctx, "c@example.com")
    client = client_for(app)
    response = client.post("/api/auth/login", json={"email": "c@example.com", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie


def test_session_token_not_stored_in_plaintext(ctx, app):
    from sentrysdr.models import UserSession
    add_user(ctx, "c@example.com")
    client = client_for(app, "c@example.com")
    token = next(iter(client.cookies.values()))
    with ctx.db() as session:
        assert token not in [s.token_hash for s in session.query(UserSession)]


# --- two-factor ------------------------------------------------------------
def test_totp_flow_and_replay_refused(ctx, app):
    add_user(ctx, "t@example.com")
    client = client_for(app, "t@example.com")
    secret = client.post("/api/me/totp/setup", json={"password": PASSWORD}).json()["secret"]
    assert client.post("/api/me/totp/enable", json={"code": "000000"}).status_code == 422
    code = pyotp.TOTP(secret).now()
    assert client.post("/api/me/totp/enable", json={"code": code}).json()["totp_enabled"] is True

    fresh = client_for(app)
    credentials = {"email": "t@example.com", "password": PASSWORD}
    response = fresh.post("/api/auth/login", json=credentials)
    assert response.status_code == 401 and response.json()["detail"] == "totp_required"
    # The code used to enable 2FA has been consumed and can't be replayed.
    assert fresh.post("/api/auth/login", json={**credentials, "totp": code}).status_code == 401
    assert "sentrysdr_session" not in fresh.cookies


def test_totp_secret_encrypted_at_rest(ctx, app):
    add_user(ctx, "t@example.com")
    client = client_for(app, "t@example.com")
    secret = client.post("/api/me/totp/setup", json={"password": PASSWORD}).json()["secret"]
    with ctx.db() as session:
        stored = session.query(User).filter_by(email="t@example.com").one().totp_secret_enc
    assert secret not in stored and ctx.secrets.open(stored) == secret


# --- administration --------------------------------------------------------
def test_admin_password_reset_forces_change_and_ends_sessions(ctx, app, admin):
    user_id = add_user(ctx, "r@example.com")
    victim = client_for(app, "r@example.com")
    temporary = admin.post(f"/api/admin/users/{user_id}/reset-password").json()["temporary_password"]
    assert victim.get("/api/me").status_code == 401

    fresh = client_for(app, "r@example.com", temporary)
    assert fresh.get("/api/sources").json()["detail"] == "password_change_required"
    changed = fresh.post("/api/me/password", json={"current_password": temporary, "new_password": "brand-new-password-1"})
    assert changed.status_code == 200
    assert fresh.get("/api/sources").status_code == 200


def test_password_change_ends_other_sessions(ctx, app):
    add_user(ctx, "p@example.com")
    one, two = client_for(app, "p@example.com"), client_for(app, "p@example.com")
    assert one.post("/api/me/password", json={"current_password": PASSWORD, "new_password": "another-new-password"}).status_code == 200
    assert one.get("/api/me").status_code == 200
    assert two.get("/api/me").status_code == 401


def test_email_change_needs_current_password(ctx, app):
    add_user(ctx, "e@example.com")
    client = client_for(app, "e@example.com")
    assert client.patch("/api/me", json={"email": "z@example.com"}).status_code == 403
    assert client.patch("/api/me", json={"email": "z@example.com", "current_password": PASSWORD}).status_code == 200


def test_last_admin_cannot_be_removed(ctx, admin):
    me = admin.get("/api/me").json()["id"]
    assert admin.patch(f"/api/admin/users/{me}", json={"role": "listener"}).status_code == 409
    assert admin.patch(f"/api/admin/users/{me}", json={"status": "disabled"}).status_code == 409


def test_disabling_user_ends_their_session(ctx, app, admin):
    user_id = add_user(ctx, "d@example.com")
    client = client_for(app, "d@example.com")
    admin.patch(f"/api/admin/users/{user_id}", json={"status": "disabled"})
    assert client.get("/api/me").status_code == 401


def test_ban_email_blocks_and_ends_session(ctx, app, admin):
    add_user(ctx, "b@example.com")
    client = client_for(app, "b@example.com")
    assert admin.post("/api/admin/bans", json={"kind": "email", "value": "B@example.com"}).status_code == 201
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "b@example.com", "password": PASSWORD}).status_code == 403


def test_ip_ban_uses_real_client_behind_tunnel(tmp_path):
    cfg = make_config(tmp_path, trusted_proxies=["127.0.0.0/8"], client_ip_header="CF-Connecting-IP")
    ctx = Context(cfg, enumerate_fn=lambda: [])
    app = create_app(ctx, run_background=False)
    add_user(ctx, ADMIN_EMAIL, role="admin")
    admin = client_for(app, ADMIN_EMAIL)
    assert admin.post("/api/admin/bans", json={"kind": "ip", "value": "198.51.100.0/24"}).status_code == 201
    client = TestClient(app, base_url="https://sentry.example.com", headers=HEADERS, client=("127.0.0.1", 40000))
    assert client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 403
    assert client.get("/api/health", headers={"CF-Connecting-IP": "203.0.113.1"}).status_code == 200


def test_admin_cannot_ban_self(admin):
    assert admin.post("/api/admin/bans", json={"kind": "email", "value": ADMIN_EMAIL}).status_code == 409


def test_audit_records_actions_with_ip(admin):
    entries = admin.get("/api/admin/audit").json()
    assert any(e["action"] == "login.success" and e["email"] == ADMIN_EMAIL and e["ip"] for e in entries)


def test_audit_search_treats_wildcards_literally(admin):
    assert admin.get("/api/admin/audit", params={"search": "%"}).json() == []


def test_api_keys_are_write_only_and_encrypted(ctx, admin):
    body = admin.patch("/api/admin/settings", json={"rdio_api_key": "super-secret-key"}).json()
    assert "super-secret-key" not in str(body) and body["rdio_api_key_set"] is True
    assert "super-secret-key" not in ctx.get_setting("rdio_api_key")
    assert ctx.get_secret("rdio_api_key") == "super-secret-key"


def test_non_http_upload_url_refused(admin):
    assert admin.patch("/api/admin/settings", json={"rdio_server": "file:///etc/passwd"}).status_code == 422
