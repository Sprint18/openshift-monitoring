from __future__ import annotations

import socket
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.patch_client import CentralPatchClient, PatchBackendError


PROJECT = Path(__file__).parents[1]


def test_native_central_patch_ui_contract() -> None:
    template = (PROJECT / "app/templates/patch_monitoring.html").read_text()
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    css = (PROJECT / "app/static/patch_monitoring.css").read_text()
    for label in ("Akış Tasarla", "Canlı İzle", "Önce / Sonra", "Oturum Geçmişi"):
        assert label in template
    for legacy in ("Agent Status", "Crash Analysis", ">Agents<", ">Events<", ">Runs<"):
        assert legacy not in template
    for field in (
        "patch-design", "patch-target", "patch-tag-mode", "patch-clusters",
        "patch-namespace-glob", "patch-namespaces", "patch-interval",
        "patch-duration",
    ):
        assert f'id="{field}"' in template
    assert "TARGET_REACHED" in template and "NOT_UPDATED" in template
    assert "REGRESSION" in template and "RECOVERED" in template
    assert "KOCCTheme.statusClass" in script
    assert "new EventSource(`/api/patch/sessions/" in script
    assert 'window.addEventListener("pagehide"' in script
    assert "PAGE_SIZE = 50" in script
    assert "flows.designs" in script
    assert "summary.counts" in script and "countSummary" in script
    assert "patch-target-table" in template
    assert "next_cursor" in script and "patch-images-pagination" in template
    assert "/containers/" in script and "patch-detail" in template
    assert '[data-theme="dark"]' in css


def test_patch_page_routes_keep_global_cluster_as_initial_hint(monkeypatch) -> None:
    monkeypatch.setattr(main, "KOCC_PATCH_ENABLED", True)
    client = TestClient(app)
    for path, marker in (
        ("/patch-monitoring", 'data-view="flow"'),
        ("/patch-monitoring/live", 'data-view="live"'),
        ("/patch-monitoring/compare", 'data-view="compare"'),
        ("/patch-monitoring/history", 'data-view="history"'),
    ):
        response = client.get(f"{path}?cluster=rmtest")
        assert response.status_code == 200
        assert marker in response.text
        assert 'data-kocc-cluster="rmtest"' in response.text


def test_patch_proxy_routes_are_explicit_and_no_arbitrary_fetch_exists() -> None:
    paths = {route.path for route in app.routes}
    expected = {
        "/api/patch/config", "/api/patch/clusters", "/api/patch/flows",
        "/api/patch/flows/preview", "/api/patch/flows/designs",
        "/api/patch/sessions", "/api/patch/sessions/{session_id}",
        "/api/patch/sessions/{session_id}/baseline",
        "/api/patch/sessions/{session_id}/start",
        "/api/patch/sessions/{session_id}/stop",
        "/api/patch/sessions/{session_id}/summary",
        "/api/patch/sessions/{session_id}/images",
        "/api/patch/sessions/{session_id}/targets",
        "/api/patch/sessions/{session_id}/changes",
        "/api/patch/sessions/{session_id}/facets",
        "/api/patch/sessions/{session_id}/containers/{row_id}",
        "/api/patch/sessions/{session_id}/stream",
    }
    assert expected <= paths
    assert "/api/patch/{resource}" not in paths


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (socket.timeout(), "timeout"),
        (urllib.error.URLError("offline"), "unavailable"),
        (ValueError("bad json"), "invalid_response"),
    ],
)
@patch("app.patch_client.urllib.request.urlopen")
def test_central_patch_client_safe_error_mapping(
    urlopen: Mock, error: Exception, code: str,
) -> None:
    urlopen.side_effect = error
    client = CentralPatchClient("http://patch-monitor:8080", 4, "")
    with pytest.raises(PatchBackendError) as caught:
        client.config()
    assert caught.value.code == code
    assert urlopen.call_args.kwargs["timeout"] == 4


def test_central_patch_database_ownership_remains_external() -> None:
    schema = (PROJECT / "app/db/schema.py").read_text().lower()
    patch_client = (PROJECT / "app/patch_client.py").read_text().lower()
    for table in ("designs", "sessions", "clusters", "rows"):
        assert f"create table {table}" not in schema
    assert "sqlite" not in patch_client
    assert "/data/patch.db" not in patch_client


@patch("app.patch_client.urllib.request.urlopen")
def test_central_patch_stream_uses_sse_contract_and_bearer(urlopen: Mock) -> None:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.readline.side_effect = [b"event: revision\n", b"data: {\"revision\":2}\n", b"\n", b""]
    urlopen.return_value = response

    client = CentralPatchClient("http://patch-monitor:8080", 7, "secret-token")
    assert list(client.stream("safe-id")) == [
        b"event: revision\n", b"data: {\"revision\":2}\n", b"\n",
    ]
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/api/v1/sessions/safe-id/stream")
    assert request.get_header("Accept") == "text/event-stream"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert urlopen.call_args.kwargs["timeout"] == 7


@pytest.mark.parametrize(
    ("http_status", "code"),
    [(409, "http_409"), (422, "http_422"), (503, "http_503")],
)
def test_central_patch_http_errors_keep_safe_status_mapping(
    http_status: int, code: str,
) -> None:
    error = urllib.error.HTTPError("http://internal.invalid", http_status, "detail", {}, None)
    mapped = CentralPatchClient._mapped_error(error)
    assert mapped.code == code
    assert mapped.http_status == http_status


@pytest.mark.parametrize(
    ("error", "status_code", "body"),
    [
        (PatchBackendError("http_409", 409), 409, b'{"error":"patch_conflict"}'),
        (PatchBackendError("http_422", 422), 422, b'{"error":"patch_validation_failed"}'),
        (PatchBackendError("timeout"), 504, b'{"error":"patch_timeout"}'),
        (PatchBackendError("unavailable"), 503, b'{"error":"patch_unavailable"}'),
    ],
)
def test_patch_proxy_returns_only_safe_errors(
    error: PatchBackendError, status_code: int, body: bytes,
) -> None:
    response = main.patch_error_response(error)
    assert response.status_code == status_code
    assert response.body == body
