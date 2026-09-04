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
from app.patch_client import PatchBackendClient, PatchBackendError


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


@patch("app.patch_client.urllib.request.urlopen")
def test_patch_client_uses_fixed_path_and_server_token(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"agent_status": "ONLINE"}).encode()
    urlopen.return_value = response
    client = PatchBackendClient("http://patch-master:8090", 5, "secret-token")

    assert client.get("summary")["agent_status"] == "ONLINE"
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://patch-master:8090/api/v1/summary"
    assert request.get_header("Authorization") == "Bearer secret-token"

    try:
        client.get("https://attacker.invalid")
    except PatchBackendError as exc:
        assert exc.code == "invalid_resource"
    else:
        raise AssertionError("arbitrary resource must be rejected")


@patch("app.patch_client.urllib.request.urlopen")
def test_patch_client_without_token_is_configured_and_omits_auth(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"agent_status":"ONLINE"}'
    urlopen.return_value = response
    client = PatchBackendClient("http://patch-master:8090", 5, "")
    assert client.configured is True
    assert client.get("summary")["agent_status"] == "ONLINE"
    assert urlopen.call_args.args[0].get_header("Authorization") is None
    assert PatchBackendClient("", 5, "token").configured is False
    assert PatchBackendClient("https://patch-master", 5, "token").configured is True


@patch("app.patch_client.urllib.request.urlopen")
def test_patch_client_start_stop_and_malformed_response(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"desired_state":"RUNNING"}'
    urlopen.return_value = response
    client = PatchBackendClient("http://patch-master:8090", 5, "token")
    assert client.start({"target_tag": "1.4.1"})["desired_state"] == "RUNNING"
    assert urlopen.call_args.args[0].full_url.endswith("/api/v1/start")
    response.read.return_value = b'{"desired_state":"STOPPED"}'
    assert client.stop()["desired_state"] == "STOPPED"
    assert urlopen.call_args.args[0].full_url.endswith("/api/v1/stop")
    response.read.return_value = b"[]"
    try:
        client.get("events")
    except PatchBackendError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("non-object response must be rejected")


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
    monkeypatch.setattr(main.patch_backend_client, "get", Mock(side_effect=PatchBackendError("unavailable")))
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/api/ai/clusters").status_code == 401
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/patch/summary").status_code == 401
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

    patch_page = client.get("/patch-monitoring")
    assert patch_page.status_code == 200
    assert "Patch Monitoring" in patch_page.text
    assert "KKBTEST1" in patch_page.text

    assert client.get("/api/patch/summary").status_code == 503
    assert client.get("/health").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/api/patch/summary").status_code == 401


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
    client = TestClient(app)
    client.post("/login", data={"username": "admin", "password": "admin"})

    mismatch = client.post("/change-password", data={
        "current_password": "admin", "new_password": "one", "confirm_password": "two",
    })
    assert mismatch.status_code == 400
    wrong = client.post("/change-password", data={
        "current_password": "bad", "new_password": "new", "confirm_password": "new",
    })
    assert wrong.status_code == 400
    changed = client.post("/change-password", data={
        "current_password": "admin", "new_password": "new", "confirm_password": "new",
    })
    assert changed.status_code == 200
    assert users.verify("admin", "admin") is False
    assert users.verify("admin", "new") is True
