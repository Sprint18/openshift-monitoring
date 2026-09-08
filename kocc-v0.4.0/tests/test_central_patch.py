from __future__ import annotations

import socket
import re
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
        "patch-designs", "patch-target", "patch-tag-mode", "patch-clusters",
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
    assert "summary.counts" in script and "summaryTotals" in script
    assert "patch-target-table" in template
    assert "next_cursor" in script and "patch-images-pagination" in template
    assert "/containers/" in script and "patch-detail" in template
    assert '[data-theme="dark"]' in css
    assert "Central Patch Monitor 0.7.2" not in template
    assert "Çoklu cluster patch geçişi, baseline ve canlı karşılaştırma" in template


def test_patch_css_is_scoped_and_global_navigation_stays_native() -> None:
    css = (PROJECT / "app/static/patch_monitoring.css").read_text()
    template = (PROJECT / "app/templates/patch_monitoring.html").read_text()
    unsafe_prefixes = ("body", "header", "nav", "main", "table", "button", "input", "select", "a")
    rules = [line.strip() for line in css.splitlines() if "{" in line]
    assert all(not rule.startswith(unsafe_prefixes) for rule in rules)
    assert '<body class="patch-monitoring">' in template
    assert '{% include "_navigation.html" %}' in template
    assert ".patch-monitoring .primary-nav > a" in css
    assert ".account-menu" not in css


def test_patch_flow_live_compare_and_history_ux_contracts() -> None:
    template = (PROJECT / "app/templates/patch_monitoring.html").read_text()
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    for text in (
        "Patch akışını oluştur", "Hazır bir akış seç", "Hedef ve kapsam",
        "İzleme davranışı", "Bu akış ne yapacak?", "Patch öncesi baseline alınır",
        "Henüz oturum yok", "Gözlenen container", "Hedef tag'de sağlıklı",
        "Eski tag'de kalan", "Tag bilinmeyen", "Ready olmayan / bekleyen",
        "Şu anda hangi image'lar var?", "Hedef sürüme geçenler",
        "Önce / sonra karşılaştırması", "Sürüm geçişi ile sağlık değişimi ayrı değerlendirilir",
        "Oturum Geçmişi",
    ):
        assert text in template or text in script
    assert "renderDesigns" in script and "applySettings" in script
    assert "session.status" in script and "data-session-action" in script
    assert "patch-no-session" in template and "showSessionArea" in script
    assert "next_cursor" in script and "PAGE_SIZE = 50" in script
    assert "session.id.slice(0, 8)" in script and "Oturumu aç →" in script
    assert "sessionStorage" in script and "localStorage" not in script


def test_patch_bootstrap_is_compatibility_first_and_errors_are_humanized() -> None:
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    config_position = script.index('await api("/api/patch/config")')
    parallel_position = script.index("await Promise.all", config_position)
    assert config_position < parallel_position
    for code in (
        "patch_timeout", "patch_unavailable", "patch_authorization_failed",
        "patch_incompatible", "patch_conflict", "patch_validation_failed",
    ):
        assert code in script
    assert "data.error ||" not in script
    assert "esc(cluster.error" not in script
    assert "önceki güvenli veri korunuyor" in script
    assert "patch-error-state" in script and "patch-content" in script


def test_patch_namespace_scope_contract_and_sandbox_exclusion() -> None:
    template = (PROJECT / "app/templates/patch_monitoring.html").read_text()
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    is_kkb_app = lambda namespace: namespace.startswith(("test-", "uat-"))
    assert is_kkb_app("test-app") is True
    assert is_kkb_app("uat-app") is True
    assert is_kkb_app("sandbox-app") is False
    assert is_kkb_app("openshift-monitoring") is False
    assert 'KKB_APPS_GLOB = "test-*,uat-*"' in script
    assert 'value="kkb" checked' in template
    assert 'value="all"' in template
    assert "sandbox-*" not in template and "sandbox-*" not in script
    assert 'namespace_glob:selectedViewScope() === "kkb" ? KKB_APPS_GLOB : ""' in script
    assert "Others" not in template


def test_compare_dom_contract_and_filter_request_are_complete() -> None:
    template = (PROJECT / "app/templates/patch_monitoring.html").read_text()
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    template_ids = set(re.findall(r'id="([^"]+)"', template))
    referenced_ids = set(re.findall(r'byId\("([^"]+)"\)', script))
    assert referenced_ids <= template_ids
    assert 'id="patch-compare-context"' in template
    assert 'byId("patch-compare-context").textContent' in script
    assert "...viewFilters(),limit:PAGE_SIZE,cursor:state.cursors.changes" in script
    assert 'search:byId("patch-compare-search").value' in script
    assert 'version_status:byId("patch-version-status").value' in script
    assert 'health_change:byId("patch-health-change").value' in script


def test_patch_operator_status_labels_and_completed_freshness_are_explicit() -> None:
    script = (PROJECT / "app/static/patch_monitoring.js").read_text()
    theme = (PROJECT / "app/static/portal_theme.js").read_text()
    for value, label in (
        ("TARGET_REACHED", "Hedef sürümde"),
        ("NOT_UPDATED", "Eski sürümde"),
        ("MIXED_VERSION", "Karışık sürüm"),
        ("REGRESSION", "Patch sonrası bozuldu"),
        ("RECOVERED", "Düzeldi"),
        ("PERSISTING_ERROR", "Sorun devam ediyor"),
        ("HEALTHY", "Sağlıklı"),
    ):
        assert f'{value}:"{label}"' in script
    assert 'PERSISTING_ERROR:"Patch öncesinde de sorun vardı."' in script
    assert '["COMPLETED","STOPPED"].includes(session.status) ? "LAST_RECORD"' in script
    assert "persistingerror" in theme and "newwitherrors" in theme


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
        (PatchBackendError("http_401", 401), 502, b'{"error":"patch_authorization_failed"}'),
        (PatchBackendError("http_403", 403), 502, b'{"error":"patch_authorization_failed"}'),
        (PatchBackendError("http_404", 404), 502, b'{"error":"patch_incompatible"}'),
        (PatchBackendError("invalid_response"), 502, b'{"error":"patch_incompatible"}'),
        (PatchBackendError("http_409", 409), 409, b'{"error":"patch_conflict"}'),
        (PatchBackendError("http_422", 422), 422, b'{"error":"patch_validation_failed"}'),
        (PatchBackendError("http_500", 500), 503, b'{"error":"patch_unavailable"}'),
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
