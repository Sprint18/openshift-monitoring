from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import SessionStore, UserRepository
from app.db.database import Database
from app.main import app
import app.main as main
from app.patch_client import CentralPatchClient, PatchBackendError


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def configure_test_auth(monkeypatch, tmp_path: Path, store: SessionStore) -> None:
    database = Database(tmp_path / "browser-auth.db")
    database.initialize()
    users = UserRepository(database)
    users.bootstrap("admin", "admin")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(main, "AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(main, "user_repository", users)
    monkeypatch.setattr(main, "session_store", store)


def test_bootstrap_password_change_survives_database_reopen(tmp_path: Path) -> None:
    database = Database(tmp_path / "kocc.db")
    database.initialize()
    users = UserRepository(database)
    assert users.bootstrap("admin", "admin") is True
    assert users.verify("admin", "admin") is True
    assert users.verify("missing", "admin") is False
    assert users.verify("admin", "bad") is False
    assert users.change_password("admin", "bad", "new-secret") is False
    assert users.change_password("admin", "admin", "new-secret") is True

    reopened = UserRepository(Database(tmp_path / "kocc.db"))
    assert reopened.verify("admin", "admin") is False
    assert reopened.verify("admin", "new-secret") is True
    assert reopened.bootstrap("admin", "admin") is False
    assert reopened.verify("admin", "new-secret") is True


def test_auth_env_added_after_auth_disabled_startup_bootstraps_user(tmp_path: Path) -> None:
    database = Database(tmp_path / "existing-kocc.db")
    database.initialize()  # Previous startup without auth configuration.
    users = UserRepository(database)
    assert main.auth_should_be_enabled(False, ("admin", "admin", "secret")) is True
    assert users.bootstrap("admin", "admin") is True
    assert users.verify("admin", "admin") is True


def test_partial_auth_configuration_never_disables_auth() -> None:
    assert main.auth_should_be_enabled(False, ("admin", "", "")) is True
    assert main.auth_should_be_enabled(False, ("", "password", "")) is True
    assert main.auth_should_be_enabled(False, ("", "", "session")) is True
    assert main.auth_should_be_enabled(False, ("", "", "")) is False


def test_partial_auth_configuration_fails_startup_closed(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_CONFIGURATION_VALUES", ("admin", "", "session"))
    monkeypatch.setattr(main, "AUTH_SESSION_SECRET", "session")
    monkeypatch.setattr(main.snapshot_repository, "initialize", Mock())

    with pytest.raises(RuntimeError, match="authentication secrets"):
        main.initialize_persistence()


def test_logout_invalidates_server_side_session() -> None:
    store = SessionStore(ttl_seconds=60)
    token = store.create("admin")
    assert store.username(token) == "admin"
    store.destroy(token)
    assert store.username(token) is None


def test_session_idle_timeout_and_activity_refresh() -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock)
    token = store.create("admin")
    clock.advance(899)
    assert store.username(token) == "admin"
    clock.advance(899)
    assert store.username(token) == "admin"
    clock.advance(900)
    assert store.username(token) is None


def test_session_lookup_without_touch_does_not_refresh_activity() -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock)
    token = store.create("admin")
    clock.advance(899)
    assert store.username(token, touch=False) == "admin"
    clock.advance(1)
    assert store.username(token) is None


def test_session_store_bounds_active_records() -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock, max_sessions=2)
    first = store.create("admin")
    clock.advance(1)
    second = store.create("admin")
    clock.advance(1)
    third = store.create("admin")
    assert store.username(first) is None
    assert store.username(second) == "admin"
    assert store.username(third) == "admin"


def test_invalid_session_timeout_configuration_uses_safe_default(monkeypatch) -> None:
    monkeypatch.setenv("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", "invalid")
    assert main.positive_integer_env("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", 900) == 900
    monkeypatch.setenv("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", "0")
    assert main.positive_integer_env("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", 900) == 900
    monkeypatch.setenv("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", "120")
    assert main.positive_integer_env("KOCC_SESSION_IDLE_TIMEOUT_SECONDS", 900) == 120


def test_independent_browser_sessions_logout_and_fixation(monkeypatch, tmp_path: Path) -> None:
    store = SessionStore(ttl_seconds=900)
    configure_test_auth(monkeypatch, tmp_path, store)
    browser_a = TestClient(app)
    browser_b = TestClient(app)

    assert browser_a.get("/", follow_redirects=False).status_code == 303
    assert browser_b.get("/?cluster=rmtest", follow_redirects=False).status_code == 303
    assert browser_b.get("/api/patch/config").status_code == 401
    assert browser_b.get("/api/ai/clusters").status_code == 401

    assert browser_a.post(
        "/login", data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    ).status_code == 303
    token_a = browser_a.cookies.get(main.AUTH_COOKIE_NAME)
    assert token_a
    assert browser_a.get("/change-password").status_code == 200
    assert browser_b.get("/change-password", follow_redirects=False).status_code == 303

    assert browser_b.post(
        "/login", data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    ).status_code == 303
    token_b = browser_b.cookies.get(main.AUTH_COOKIE_NAME)
    assert token_b and token_b != token_a

    assert browser_a.post("/logout", follow_redirects=False).status_code == 303
    assert browser_a.get("/change-password", follow_redirects=False).status_code == 303
    assert browser_b.get("/change-password").status_code == 200

    old_b_token = token_b.rpartition(".")[0]
    assert browser_b.post(
        "/login", data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    ).status_code == 303
    replacement = browser_b.cookies.get(main.AUTH_COOKIE_NAME)
    assert replacement and replacement != token_b
    assert store.username(old_b_token) is None


def test_expired_cookie_redirects_html_and_rejects_api(monkeypatch, tmp_path: Path) -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock)
    configure_test_auth(monkeypatch, tmp_path, store)
    client = TestClient(app)
    client.post("/login", data={"username": "admin", "password": "admin"})
    assert client.get("/change-password").status_code == 200
    clock.advance(901)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/patch/config").status_code == 401


def test_background_patch_polling_does_not_extend_idle_session(
    monkeypatch, tmp_path: Path,
) -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock)
    configure_test_auth(monkeypatch, tmp_path, store)
    browser = TestClient(app)
    browser.post("/login", data={"username": "admin", "password": "admin"})

    clock.advance(899)
    # The backend may be disabled, but authentication succeeds without touching
    # the idle timestamp for this automatic API request.
    assert browser.get("/api/patch/config").status_code != 401
    clock.advance(2)
    assert browser.get("/api/patch/config").status_code == 401
    assert browser.get("/", follow_redirects=False).status_code == 303


def test_explicit_browser_activity_refreshes_idle_session(
    monkeypatch, tmp_path: Path,
) -> None:
    clock = FakeClock()
    store = SessionStore(ttl_seconds=900, clock=clock)
    configure_test_auth(monkeypatch, tmp_path, store)
    browser = TestClient(app)
    browser.post("/login", data={"username": "admin", "password": "admin"})

    clock.advance(899)
    assert browser.post("/api/session/activity").status_code == 200
    clock.advance(899)
    assert browser.post("/api/session/activity").status_code == 200
    clock.advance(901)
    assert browser.post("/api/session/activity").status_code == 401


def test_patch_polling_stops_and_redirects_after_unauthorized_response() -> None:
    source = (Path(__file__).parents[1] / "app/static/patch_monitoring.js").read_text()
    assert "expired: false" in source
    assert "response.status === 401" in source
    assert "clearInterval(state.timer)" in source
    assert "state.expired || state.refreshing" in source
    assert "location.assign(`/login?next=${encodeURIComponent" in source


@patch("app.patch_client.urllib.request.urlopen")
def test_central_patch_client_uses_allowlisted_contract_and_server_token(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"items": []}).encode()
    urlopen.return_value = response
    client = CentralPatchClient("http://patch-monitor:8080", 5, "secret-token")

    assert client.clusters() == {"items": []}
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://patch-monitor:8080/api/v1/clusters"
    assert request.get_header("Authorization") == "Bearer secret-token"
    with pytest.raises(PatchBackendError, match="invalid_resource"):
        client.session_view("safe-id", "arbitrary")
    with pytest.raises(PatchBackendError, match="invalid_resource"):
        client.session("../../secret")


@patch("app.patch_client.urllib.request.urlopen")
def test_central_patch_client_without_token_omits_authorization(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"templates":{},"designs":[]}'
    urlopen.return_value = response
    client = CentralPatchClient("http://patch-monitor:8080", 5, "")
    assert client.configured is True
    assert client.flows()["templates"] == {}
    assert urlopen.call_args.args[0].get_header("Authorization") is None
    assert CentralPatchClient("", 5, "token").configured is False
    assert CentralPatchClient("https://patch-monitor", 5, "token").configured is True


@patch("app.patch_client.urllib.request.urlopen")
def test_central_patch_client_complete_api_mapping_and_query_allowlist(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"ok":true}'
    urlopen.return_value = response
    client = CentralPatchClient("http://patch-monitor:8080", 5, "token")
    calls = [
        (client.config, (), "/api/v1/config"),
        (client.clusters, (), "/api/v1/clusters"),
        (client.flows, (), "/api/v1/flows"),
        (client.preview, ({"target_tag": "1.4.1"},), "/api/v1/flows/preview"),
        (client.save_design, ({"name": "test"},), "/api/v1/flows/designs"),
        (client.sessions, (), "/api/v1/sessions"),
        (client.create_session, ({"target_tag": "1.4.1"},), "/api/v1/sessions"),
        (client.session, ("abc123",), "/api/v1/sessions/abc123"),
        (client.session_action, ("abc123", "baseline"), "/api/v1/sessions/abc123/baseline"),
        (client.session_action, ("abc123", "start"), "/api/v1/sessions/abc123/start"),
        (client.session_action, ("abc123", "stop"), "/api/v1/sessions/abc123/stop"),
        (client.container, ("abc123", "row-1"), "/api/v1/sessions/abc123/containers/row-1"),
    ]
    for operation, args, suffix in calls:
        assert operation(*args) == {"ok": True}
        assert urlopen.call_args.args[0].full_url.endswith(suffix)
    for resource in ("summary", "images", "targets", "changes", "facets"):
        client.session_view("abc123", resource, {"limit": 50, "secret": "no"})
        url = urlopen.call_args.args[0].full_url
        assert f"/api/v1/sessions/abc123/{resource}" in url
        assert "limit=50" in url and "secret" not in url
    response.read.return_value = b"[]"
    with pytest.raises(PatchBackendError, match="invalid_response"):
        client.config()


def test_auth_boundary_and_patch_failure_isolation(monkeypatch, tmp_path: Path) -> None:
    database = Database(tmp_path / "auth.db")
    database.initialize()
    users = UserRepository(database)
    users.bootstrap("admin", "admin")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(main, "AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(main, "user_repository", users)
    monkeypatch.setattr(main, "session_store", SessionStore())
    monkeypatch.setattr(main, "KOCC_PATCH_ENABLED", True)
    monkeypatch.setattr(main.patch_backend_client, "config", Mock(side_effect=PatchBackendError("unavailable")))
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/api/ai/clusters").status_code == 401
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/patch/config").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/?cluster=rmtest", follow_redirects=False).status_code == 303
    assert client.get("/patch-monitoring", follow_redirects=False).status_code == 303
    assert client.get("/ai-assistant", follow_redirects=False).status_code == 303
    assert client.get("/static/kkb-turuncu-lacivert-logo.png").status_code == 200

    bad = client.post("/login", data={"username": "admin", "password": "bad"})
    assert bad.status_code == 401
    assert "Kullanıcı adı veya parola hatalı" in bad.text
    login = client.post(
        "/login", data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    assert "Max-Age" not in login.headers["set-cookie"]

    patch_page = client.get("/patch-monitoring")
    assert patch_page.status_code == 200
    assert "Patch Monitoring" in patch_page.text
    assert "Çoklu cluster patch geçişi, baseline ve canlı karşılaştırma" in patch_page.text
    assert "Central Patch Monitor 0.7.2" not in patch_page.text
    navigation = patch_page.text.split(
        'aria-label="Dashboard navigation"', 1
    )[1].split("</nav>", 1)[0]
    assert "admin" in navigation
    assert "Parola Değiştir" in navigation
    assert '<form method="post" action="/logout">' in navigation
    assert ">Hesap<" not in navigation

    assert client.get("/api/patch/config").status_code == 503
    assert client.get("/health").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/api/patch/config").status_code == 401


def test_password_change_validation(monkeypatch, tmp_path: Path) -> None:
    database = Database(tmp_path / "auth.db")
    database.initialize()
    users = UserRepository(database)
    users.bootstrap("admin", "admin")
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "AUTH_COOKIE_SECURE", False)
    monkeypatch.setattr(main, "AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(main, "user_repository", users)
    monkeypatch.setattr(main, "session_store", SessionStore())
    change_password = Mock(wraps=users.change_password)
    monkeypatch.setattr(users, "change_password", change_password)
    client = TestClient(app)
    client.post("/login", data={"username": "admin", "password": "admin"})

    mismatch = client.post("/change-password", data={
        "current_password": "admin", "new_password": "one", "confirm_password": "two",
    })
    assert mismatch.status_code == 400
    change_password.assert_not_called()
    wrong = client.post("/change-password", data={
        "current_password": "bad", "new_password": "new", "confirm_password": "new",
    })
    assert wrong.status_code == 400
    assert change_password.call_count == 1
    change_password.reset_mock()
    changed = client.post("/change-password", data={
        "current_password": "admin", "new_password": "new", "confirm_password": "new",
    }, follow_redirects=False)
    assert changed.status_code == 303
    assert changed.headers["location"] == "/change-password?changed=1"
    change_password.assert_called_once_with("admin", "admin", "new")
    success = client.get(changed.headers["location"])
    assert success.status_code == 200
    assert "Parolanız başarıyla değiştirildi." in success.text
    assert users.verify("admin", "admin") is False
    assert users.verify("admin", "new") is True
