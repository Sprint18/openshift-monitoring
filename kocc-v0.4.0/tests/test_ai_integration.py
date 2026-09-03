from __future__ import annotations

import json
import socket
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.ai_client import AIBackendClient, AIBackendError
from app.main import app


client = TestClient(app)


class FakeResponse:
    def __init__(self, body: object) -> None:
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def test_ai_assistant_page_renders_with_shared_navigation() -> None:
    response = client.get("/ai-assistant?cluster=kkbtest")
    assert response.status_code == 200
    assert "KKB ShiftLight AI" in response.text
    assert "OpenShift Operasyon Asistanı" in response.text
    assert "/static/kkb-turuncu-lacivert-logo.png" in response.text
    assert "Read-only" in response.text
    assert 'aria-label="Dashboard navigation"' in response.text
    assert 'data-open-on-load="true"' in response.text


@patch("app.main.ai_backend_client")
def test_ai_cluster_discovery_proxy_filters_supported_clusters(ai_client: Mock) -> None:
    ai_client.clusters.return_value = [
        {"id": "kkbtest", "name": "KKB TEST", "enabled": True},
        {"id": "rmtest", "name": "RM TEST", "enabled": True},
        {"id": "disabled", "name": "Disabled", "enabled": False},
    ]
    response = client.get("/api/ai/clusters")
    assert response.status_code == 200
    assert response.json() == {
        "clusters": [
            {"id": "kkbtest", "name": "KKB TEST", "enabled": True},
            {"id": "rmtest", "name": "RM TEST", "enabled": True},
        ]
    }


@patch("app.main.ai_backend_client")
def test_ai_chat_proxy_preserves_safe_contract(ai_client: Mock) -> None:
    ai_client.chat.return_value = {
        "cluster": "kkbtest",
        "answer": "Cluster healthy",
        "tool_calls": [{"name": "resources_list", "status": "success"}],
        "evidence": [{"tool": "resources_list", "status": "success"}],
    }
    response = client.post("/api/ai/chat", json={
        "cluster": "kkbtest", "message": "Cluster health",
    })
    assert response.status_code == 200
    assert response.json() == ai_client.chat.return_value
    ai_client.chat.assert_called_once_with(
        "kkbtest", "Cluster health", target_cluster_ids=None,
        conversation_scope="auto",
    )


@patch("app.main.ai_backend_client")
def test_ai_chat_proxy_round_trips_safe_conversation_context(
    ai_client: Mock,
) -> None:
    context = {
        "active_cluster_ids": ["kkbtest"],
        "last_resource_kind": "Namespace",
        "last_filter_type": "prefix",
        "last_filter_value": "uat",
        "last_operation": "count",
        "active_entity_kind": "Namespace",
        "active_entity_name": "uat-zauh",
        "active_inspection": {
            "inspection_type": "pod_health", "resource_kind": "Pod",
            "cluster_id": "kkbtest", "namespace": "uat-zauh",
            "pod_count": 2, "ready_count": 0, "non_ready_count": 2,
            "total_restarts": 252, "max_restart_count": 237,
            "problematic_pod_names": ["pod-a", "pod-b"],
            "observed_at": "2026-09-03T10:00:00+00:00",
        },
    }
    ai_client.chat.return_value = {
        "cluster": "kkbtest", "clusters": [{"id": "kkbtest", "name": "KKB TEST"}],
        "answer": "safe", "tool_calls": [], "evidence": [],
        "conversation_context": context,
    }
    response = client.post("/api/ai/chat", json={
        "message": "bu namespace'leri listele",
        "conversation_context": context,
        "recent_turns": [{"role": "user", "content": "önceki soru"}],
    })
    assert response.status_code == 200
    assert response.json()["conversation_context"] == context
    assert ai_client.chat.call_args.kwargs["conversation_context"] == context
    assert ai_client.chat.call_args.kwargs["recent_turns"] == [
        {"role": "user", "content": "önceki soru"}
    ]


def test_ai_proxy_preserves_phase5b3_bounded_recent_turn_window() -> None:
    turns = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(30)
    ]
    result = AIBackendClient._recent_turns(turns)
    assert len(result) == 24
    assert result[0]["content"] == "6"


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
@patch("app.main.ai_backend_client")
def test_ai_chat_rejects_empty_message(ai_client: Mock, message: str) -> None:
    response = client.post("/api/ai/chat", json={
        "cluster": "kkbtest", "message": message,
    })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_message"
    ai_client.chat.assert_not_called()


@patch("app.main.ai_backend_client")
def test_ai_chat_ignores_legacy_browser_cluster_for_routing(ai_client: Mock) -> None:
    ai_client.chat.return_value = {
        "cluster": "rmtest", "answer": "RMTEST", "tool_calls": [], "evidence": [],
    }
    response = client.post("/api/ai/chat", json={
        "cluster": "unknown", "message": "RMTEST'e bak",
    })
    assert response.status_code == 200
    ai_client.chat.assert_called_once_with(
        "kkbtest", "RMTEST'e bak", target_cluster_ids=None,
        conversation_scope="auto",
    )


@patch("app.main.ai_backend_client")
def test_ai_chat_proxy_does_not_forward_dashboard_context_hint(ai_client: Mock) -> None:
    ai_client.chat.return_value = {
        "cluster": "rmtest", "answer": "RMTEST", "tool_calls": [], "evidence": [],
    }
    response = client.post("/api/ai/chat", json={
        "message": "cluster durumuna bak", "context_cluster_id": "rmtest",
    })
    assert response.status_code == 200
    ai_client.chat.assert_called_once_with(
        "kkbtest", "cluster durumuna bak", target_cluster_ids=None,
        conversation_scope="auto",
    )

    ai_client.reset_mock()
    client.post("/api/ai/chat", json={
        "message": "cluster durumuna bak", "context_cluster_id": "arbitrary-url",
    })
    ai_client.chat.assert_called_once_with(
        "kkbtest", "cluster durumuna bak", target_cluster_ids=None,
        conversation_scope="auto",
    )


@pytest.mark.parametrize(("code", "status", "error"), [
    ("unavailable", 503, "ai_unavailable"),
    ("timeout", 504, "ai_timeout"),
    ("http_503", 503, "ai_unavailable"),
    ("invalid_response", 502, "ai_invalid_response"),
])
@patch("app.main.ai_backend_client")
def test_ai_chat_proxy_returns_controlled_errors(
    ai_client: Mock, code: str, status: int, error: str
) -> None:
    ai_client.chat.side_effect = AIBackendError(code)
    response = client.post("/api/ai/chat", json={
        "cluster": "kkbtest", "message": "health",
    })
    assert response.status_code == status
    assert response.json()["error"] == error
    assert "svc.cluster.local" not in response.text


@patch("app.ai_client.urllib.request.urlopen")
def test_ai_client_handles_connection_timeout_and_malformed_json(urlopen: Mock) -> None:
    backend = AIBackendClient("http://ai.internal:8080", 90)
    urlopen.side_effect = urllib.error.URLError(socket.timeout("private detail"))
    with pytest.raises(AIBackendError, match="timeout"):
        backend.clusters()

    urlopen.side_effect = None
    urlopen.return_value = FakeResponse(b"not-json")
    with pytest.raises(AIBackendError, match="invalid_response"):
        backend.clusters()


@pytest.mark.parametrize("url", ["", "not-a-url", "file:///tmp/socket"])
def test_ai_client_without_valid_configured_url_is_controlled(url: str) -> None:
    with pytest.raises(AIBackendError, match="unavailable"):
        AIBackendClient(url, 90).clusters()


@patch("app.ai_client.urllib.request.urlopen")
def test_ai_client_sanitizes_chat_response_metadata(urlopen: Mock) -> None:
    urlopen.return_value = FakeResponse({
        "cluster": "kkbtest",
        "answer": "Safe answer",
        "tool_calls": [{"name": "nodes_top", "status": "success", "raw": "hidden"}],
        "evidence": [{"tool": "nodes_top", "status": "success", "arguments": "hidden"}],
        "internal_url": "must-not-pass",
    })
    result = AIBackendClient("http://ai.internal:8080", 90).chat(
        "kkbtest", "health"
    )
    assert result == {
        "cluster": "kkbtest",
        "clusters": [],
        "answer": "Safe answer",
        "tool_calls": [{"name": "nodes_top", "status": "success"}],
        "evidence": [{"tool": "nodes_top", "status": "success"}],
    }


@patch("app.ai_client.urllib.request.urlopen")
def test_ai_client_preserves_safe_temporary_cluster_choices(urlopen: Mock) -> None:
    urlopen.return_value = FakeResponse({
        "answer": "Hangi cluster?", "needs_cluster_selection": True,
        "clarification_id": "79f71180-9f62-4bdb-83c2-0ba4458ac878",
        "cluster_choices": [
            {"id": "kkbtest", "name": "KKB TEST"},
            {"id": "rmtest", "name": "RMTEST"},
        ],
        "allow_all": True,
    })
    result = AIBackendClient("http://ai.internal:8080", 90).chat(
        "kkbtest", "cluster durumuna bak"
    )
    assert result["needs_cluster_selection"] is True
    assert result["clarification_id"] == "79f71180-9f62-4bdb-83c2-0ba4458ac878"
    assert [item["id"] for item in result["cluster_choices"]] == [
        "kkbtest", "rmtest",
    ]
    assert "url" not in str(result).lower()


@patch("app.ai_client.urllib.request.urlopen")
def test_ai_client_sends_only_canonical_context_cluster_id(urlopen: Mock) -> None:
    urlopen.return_value = FakeResponse({
        "cluster": "rmtest", "answer": "Safe", "tool_calls": [], "evidence": [],
    })
    AIBackendClient("http://ai.internal:8080", 90).chat(
        "kkbtest", "health", context_cluster_id="rmtest"
    )
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload == {
        "cluster": "kkbtest", "message": "health",
        "context_cluster_id": "rmtest",
    }
    assert "mcp" not in json.dumps(payload).lower()


@patch("app.ai_client.urllib.request.urlopen")
def test_ai_client_preserves_only_safe_numeric_evidence_facts(urlopen: Mock) -> None:
    urlopen.return_value = FakeResponse({
        "cluster": "kkbtest",
        "answer": "34 operators",
        "tool_calls": [{"name": "resources_list", "status": "success"}],
        "evidence": [{
            "cluster": "kkbtest",
            "tool": "resources_list", "status": "success",
            "facts": {
                "resource_count": 34,
                "degraded_true_count": 0,
                "available_false_count": 0,
                "progressing_true_count": 1,
                "raw_resources": ["must-not-pass"],
                "apiVersion": "must-not-pass",
            },
        }],
    })
    result = AIBackendClient("http://ai.internal:8080", 90).chat("kkbtest", "health")
    assert result["evidence"] == [{
        "cluster": "kkbtest",
        "tool": "resources_list", "status": "success",
        "facts": {
            "resource_count": 34,
            "degraded_true_count": 0,
            "available_false_count": 0,
            "progressing_true_count": 1,
        },
    }]


def safe_chat_response() -> dict:
    return {
        "answer": "Safe answer",
        "tool_calls": [],
        "evidence": [],
    }


@patch("app.ai_client.time.sleep")
@patch("app.ai_client.urllib.request.urlopen")
def test_chat_retries_one_pre_response_url_error_then_succeeds(
    urlopen: Mock, sleep: Mock
) -> None:
    urlopen.side_effect = [
        urllib.error.URLError(ConnectionRefusedError("refused")),
        FakeResponse(safe_chat_response()),
    ]
    result = AIBackendClient("http://ai.internal:8080", 90).chat("kkbtest", "health")
    assert result["answer"] == "Safe answer"
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(0.2)


@patch("app.ai_client.time.sleep")
@patch("app.ai_client.urllib.request.urlopen")
def test_chat_transport_retry_is_bounded_to_one(
    urlopen: Mock, sleep: Mock
) -> None:
    urlopen.side_effect = urllib.error.URLError(ConnectionResetError("reset"))
    with pytest.raises(AIBackendError, match="unavailable"):
        AIBackendClient("http://ai.internal:8080", 90).chat("kkbtest", "health")
    assert urlopen.call_count == 2
    assert sleep.call_count == 1


@pytest.mark.parametrize("status", [400, 503])
@patch("app.ai_client.time.sleep")
@patch("app.ai_client.urllib.request.urlopen")
def test_chat_http_errors_are_not_retried(
    urlopen: Mock, sleep: Mock, status: int
) -> None:
    urlopen.side_effect = urllib.error.HTTPError(
        "http://ai.internal/api/v1/chat", status, "error", {}, None
    )
    with pytest.raises(AIBackendError, match=f"http_{status}"):
        AIBackendClient("http://ai.internal:8080", 90).chat("kkbtest", "health")
    assert urlopen.call_count == 1
    sleep.assert_not_called()


@patch("app.ai_client.time.sleep")
@patch("app.ai_client.urllib.request.urlopen")
def test_chat_timeout_is_not_retried(urlopen: Mock, sleep: Mock) -> None:
    urlopen.side_effect = socket.timeout("slow")
    with pytest.raises(AIBackendError, match="timeout"):
        AIBackendClient("http://ai.internal:8080", 90).chat("kkbtest", "health")
    assert urlopen.call_count == 1
    sleep.assert_not_called()


def shiftlight_source() -> str:
    return (Path(__file__).parents[1] / "app/static/shiftlight_assistant.js").read_text()


def shiftlight_partial() -> str:
    return (Path(__file__).parents[1] / "app/templates/_shiftlight_assistant.html").read_text()


def test_ai_template_uses_safe_text_rendering_and_only_kocc_endpoints() -> None:
    source = shiftlight_source()
    assert "innerHTML" not in source
    assert "textContent" in source
    assert 'fetchJson("/api/ai/chat"' in source
    assert 'fetchJson("/api/ai/clusters")' not in source
    assert "svc.cluster.local" not in source
    assert "data.evidence" in source
    assert "item.tool" in source


def test_ai_template_checks_status_and_content_type_before_json() -> None:
    source = shiftlight_source()
    assert 'response.headers.get("Content-Type")' in source
    assert 'type.startsWith("application/json")' in source
    assert "if (!response.ok)" in source
    assert "data = await response.json()" in source
    assert "data = null" in source
    assert "response.text()" not in source
    assert "error.message" not in source


def test_ai_template_maps_http_and_network_errors_to_safe_messages() -> None:
    source = shiftlight_source()
    assert 'status === 400 ? "request"' in source
    assert 'status === 502 || status === 503 ? "unavailable"' in source
    assert 'status === 504 ? "timeout"' in source
    assert "İstek ShiftLight AI tarafından işlenemedi." in source
    assert "ShiftLight AI şu anda kullanılamıyor." in source
    assert "ShiftLight AI yanıtı zaman aşımına uğradı." in source
    assert 'error.name === "AbortError" ? "timeout" : "unavailable"' in source
    assert "Unexpected token" not in source
    assert "Gateway Timeout" not in source
    assert "<html>" not in source


def test_ai_template_preserves_loading_deduplication_and_evidence() -> None:
    source = shiftlight_source()
    assert "if (requestPending" in source
    assert "setPending(true)" in source
    assert "sendButton.disabled = pending" in source
    assert "safeEvidence(data.evidence)" in source
    assert 'item.status !== "success"' in source


def test_ai_template_is_a_scrollable_chat_workspace() -> None:
    partial = shiftlight_partial()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    source = shiftlight_source()
    assert 'class="shiftlight-drawer"' in partial
    assert ".shiftlight-drawer" in css
    assert "display: flex" in css
    assert "flex-direction: column" in css
    assert "height: 100dvh" in css
    assert ".shiftlight-conversation { display:flex; flex:1 1 auto" in css
    assert "min-height:0" in css or "min-height: 0" in css
    assert ".shiftlight-composer { position:relative; bottom:auto; flex:0 0 auto" in css
    assert "overflow-y:auto" in css
    drawer_css = css[css.index(".shiftlight-drawer {"):css.index(".shiftlight-drawer.open")]
    assert "overflow: hidden" in drawer_css
    assert partial.index('id="shiftlight-conversation"') < partial.index('id="shiftlight-form"')
    assert ".shiftlight-drawer.open.fullscreen" in css and "height:94dvh" in css
    assert "conversation.scrollHeight" in source


def test_history_panel_is_bounded_without_competing_with_composer() -> None:
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    partial = shiftlight_partial()
    source = shiftlight_source()
    history_css = css[
        css.index(".shiftlight-history {"):css.index(".shiftlight-history-title")
    ]
    assert "flex:0 1 auto" in history_css
    assert "min-height:0" in history_css
    assert "max-height:min(220px,30dvh)" in history_css
    assert "overflow-y:auto" in history_css
    assert "overscroll-behavior:contain" in history_css
    assert partial.index('id="shiftlight-history"') < partial.index(
        'id="shiftlight-conversation"'
    ) < partial.index('id="shiftlight-form"')
    assert ".shiftlight-conversation { display:flex; flex:1 1 auto" in css
    assert ".shiftlight-composer { position:relative; bottom:auto; flex:0 0 auto" in css
    assert ".shiftlight-drawer.open.fullscreen" in css and "height:94dvh" in css
    assert "conversation.scrollTop" in source
    assert "conversation.scrollHeight" in source


def test_ai_template_keeps_composer_and_keyboard_behavior() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    assert '<form id="shiftlight-form" class="shiftlight-composer">' in partial
    assert '<textarea id="shiftlight-message"' in partial
    assert 'event.key === "Enter" && !event.shiftKey' in source
    assert "event.preventDefault();" in source
    assert "form.requestSubmit();" in source
    assert "!text.trim()" in source


def test_ai_template_new_chat_defaults_to_auto_scope() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    assert 'id="shiftlight-new"' in partial
    assert "conversation.replaceChildren()" in source
    assert 'newButton.addEventListener("click"' in source
    assert 'scope: "auto"' in source
    assert 'scopeSelect.addEventListener("change"' in source
    assert "const startNew = ()" in source
    assert "newButton.disabled = pending" in source
    assert "suggestions.forEach" in source


def test_ai_template_has_safe_markdown_and_non_blank_fallback() -> None:
    source = shiftlight_source()
    assert "renderMarkdown" in source
    assert "appendInline" in source
    assert 'document.createElement("table")' in source
    assert 'document.createElement("pre")' in source
    assert 'document.createElement(`h${heading[1].length}`)' in source
    assert 'document.createElement("strong")' not in source  # created through audited helper
    assert "javascript:" not in source
    assert "^https?:\\/\\/" in source
    assert "target.textContent =" in source
    assert "element.innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source


def test_ai_template_loading_and_http_200_blank_regression() -> None:
    source = shiftlight_source()
    assert "ShiftLight düşünüyor" in source
    assert 'text: data.answer, evidence: safeEvidence(data.evidence), status: "success"' in source
    assert '!data.answer.trim()' in source
    assert "renderer produced no content" in source


def test_ai_template_brand_context_evidence_and_responsive_hooks() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert 'id="shiftlight-scope"' in partial
    assert '<option value="auto">Auto</option>' in partial
    assert 'id="shiftlight-new"' in partial
    assert "Kullanılan cluster verileri" in source
    assert "shiftlight-evidence" in source
    assert "@media (max-width: 620px)" in css
    assert "shiftlight-composer" in partial
    assert "shiftlight-empty" in source


@pytest.mark.parametrize("path", ["/", "/workloads", "/health-overview"])
@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_global_shiftlight_drawer_is_shared_across_portal_pages(
    _new_client: Mock, collector_class: Mock, path: str
) -> None:
    from tests.test_main import dashboard_payload

    collector_class.return_value.collect_dashboard.return_value = dashboard_payload()
    response = client.get(f"{path}?cluster=kkbtest")
    assert response.status_code == 200
    assert response.text.count('id="shiftlight-root"') == 1
    assert 'id="shiftlight-launcher"' in response.text
    assert 'id="shiftlight-drawer"' in response.text


def test_shiftlight_nav_is_deemphasized_and_legacy_route_opens_drawer() -> None:
    navigation = (Path(__file__).parents[1] / "app/templates/_navigation.html").read_text()
    response = client.get("/ai-assistant?cluster=kkbtest")
    assert '"AI Assistant"' not in navigation
    assert 'data-open-on-load="true"' in response.text
    assert "/ai-assistant" not in navigation


def test_shiftlight_welcome_nudge_and_accessibility_hooks() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert "Merhaba 👋" in partial
    assert "Ben KKB ShiftLight AI." in partial
    assert 'aria-controls="shiftlight-drawer"' in partial
    assert "sessionStorage.getItem(NUDGE_KEY)" in source
    assert "window.setTimeout(() => collapseWelcome(run), 6800)" in source
    assert "prefers-reduced-motion: reduce" in css
    assert "https://" not in partial and "cdn" not in partial.lower()
    assert 'role="dialog"' in partial
    assert 'aria-modal="false"' in partial


def test_shiftlight_mascot_flight_reuses_first_session_welcome_state() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert "Merhaba 👋" in partial
    assert "Ben KKB ShiftLight AI." in partial
    assert "Size nasıl yardımcı olabilirim?" in partial
    assert 'sessionStorage.getItem(NUDGE_KEY) === "true"' in source
    assert 'sessionStorage.setItem(NUDGE_KEY, "true")' in source
    assert "localStorage" not in source
    assert 'setFlightState("flight-ready")' in source
    assert 'setFlightState("flying")' in source
    assert "mobile ? 650 : 750" in source
    assert "collapseWelcome(run), 6800" in source
    assert 'launcher.addEventListener("click", openDrawer)' in source
    assert 'flightMascot.addEventListener("click", openDrawer)' in source
    assert "@keyframes shiftlight-flight" in css
    assert "pointer-events:none" in css
    assert 'class="shiftlight-launcher-mascot"' in partial
    assert 'id="shiftlight-flight"' in partial
    assert 'id="shiftlight-flight" class="shiftlight-flight" type="button" hidden data-state="idle"' in partial


def test_shiftlight_flight_respects_reduced_motion_and_existing_layouts() -> None:
    source = shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert 'window.matchMedia("(prefers-reduced-motion: reduce)").matches' in source
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".shiftlight-flight[data-state]" in reduced
    assert "animation:none" in reduced
    assert "transform:none" in reduced
    assert 'drawer.classList.toggle("fullscreen", expanded)' in source
    assert 'id="shiftlight-drawer"' in shiftlight_partial()
    assert "https://" not in shiftlight_partial()


def test_shiftlight_welcome_lands_greets_and_compacts_to_launcher() -> None:
    source = shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert 'setFlightState("landed")' in source
    assert "const landMascot = (run, settle = 320)" in source
    assert 'nudge.classList.add("visible")' in source
    assert 'setFlightState("resting")' in source
    assert 'launcher.classList.remove("welcome-hidden")' in source
    assert "}, 420)" in source
    assert "@keyframes shiftlight-land" in css
    assert "@keyframes shiftlight-compact" in css
    flight = css[css.index("@keyframes shiftlight-flight"):css.index("@keyframes shiftlight-land")]
    assert all(step in flight for step in ("0%", "20%", "45%", "70%", "100%"))
    assert "translate3d" in flight
    assert "width:128px; height:128px" in css
    assert "width:62px; height:62px" in css


def test_shiftlight_runtime_flight_paints_ready_state_before_animation() -> None:
    source = shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    ready = source.index('setFlightState("flight-ready")')
    raf = source.index("requestAnimationFrame(() => requestAnimationFrame", ready)
    flying = source.index('setFlightState("flying")', raf)
    assert ready < raf < flying
    assert '[data-state="flight-ready"]' in css
    assert "translate3d(190px,120px,0)" in css
    assert "translate3d(-360px,-90px,0)" in css
    assert 'flightMascot.addEventListener("animationend"' in source
    assert 'event.animationName' in source
    assert 'flightMascot.dataset.state === "flying"' in source
    assert "landMascot(welcomeRun)" in source
    flight_block = source[source.index('setFlightState("flight-ready")'):source.index("window.shiftLightReplayWelcome")]
    assert "mobile ? 1250 : 1900" not in flight_block


def test_shiftlight_greeting_follows_landing_and_resting_follows_greeting() -> None:
    source = shiftlight_source()
    landed = source.index('setFlightState("landed")')
    reveal = source.index("revealNudge(run)", landed)
    assert landed < reveal
    reveal_block = source[source.index("const revealNudge"):source.index("const landMascot")]
    collapse_block = source[source.index("const collapseWelcome"):source.index("const revealNudge")]
    assert 'setFlightState("greeting")' in reveal_block
    assert "collapseWelcome(run), 6800" in reveal_block
    assert 'setFlightState("resting")' in collapse_block


def test_shiftlight_welcome_replay_isolated_and_open_drawer_cancels_sequence() -> None:
    source = shiftlight_source()
    assert "window.shiftLightReplayWelcome" in source
    replay = source[source.index("window.shiftLightReplayWelcome"):source.index("const errorKind")]
    assert "sessionStorage.removeItem(NUDGE_KEY)" in replay
    assert "showNudge(true)" in replay
    assert "SESSION_KEY" not in replay
    assert "localStorage" not in replay
    open_drawer = source[source.index("const openDrawer"):source.index("const setFullscreen")]
    assert "cancelWelcome()" in open_drawer
    assert 'flightMascot.hidden = true' in source
    assert 'drawer.classList.contains("open") || fullscreen' in source
    assert 'pointer-events:none' in (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()


def test_shiftlight_session_history_is_bounded_minimal_and_routing_free() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    assert 'const SESSION_KEY = "kocc.shiftlight.conversations.v1"' in source
    assert "const STORE_VERSION = 2" in source
    assert "const MAX_CONVERSATIONS = 10" in source
    assert "sessionStorage.setItem(SESSION_KEY" in source
    assert "localStorage" not in source
    assert "activeConversationId" in source
    assert "conversations: []" in source
    assert "store.conversations.push(item)" in source
    assert "store.conversations = store.conversations.slice(-MAX_CONVERSATIONS)" in source
    conversation_sanitizer = source[
        source.index("const safeConversation"):source.index("const safeStore")
    ]
    assert "item.cluster" not in conversation_sanitizer
    assert "scope: safeScope(value.scope), context: safeChatContext(value.context), summary, messages" in source
    assert 'id="shiftlight-history-list"' in partial


def test_shiftlight_sends_bounded_safe_history_and_structured_context() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    assert "const MAX_CONTEXT_TURNS = 24" in source
    assert "const MAX_CONTEXT_CHARS = 16000" in source
    assert "const recentTurns = (conversationItem)" in source
    assert "recent_turns: history" in source
    assert "conversation_summary: current.summary" in source
    assert "const buildConversationSummary = (conversationItem)" in source
    assert "conversation_context: safeChatContext(current.context)" in source
    assert "store.conversations.find((item) => item.id === turn.conversationId)" in source
    assert "responseConversation.context = safeChatContext(data.conversation_context)" in source
    assert "result.active_inspection = safeInspection" in source
    assert "context: {}" in source[source.index("const emptyConversation"):source.index("const emptyStore")]
    assert 'summary: ""' in source[source.index("const emptyConversation"):source.index("const emptyStore")]
    assert "context: safeChatContext(value.context)" in source
    context_block = source[
        source.index("const safeChatContext"):source.index("const emptyConversation")
    ]
    assert "evidence" not in context_block
    assert "tool_calls" not in context_block
    assert "Sohbetler" in partial
    assert "raw MCP" not in source
    assert "kubeconfig" not in source
    assert "api-token" not in source
    assert "tool_calls" not in source


def test_shiftlight_scope_is_conversation_level_and_dashboard_selector_remains() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    dashboard = (Path(__file__).parents[1] / "app/templates/index.html").read_text()
    assert 'id="shiftlight-scope"' in partial
    assert 'body.conversation_scope = currentScope' in source
    assert 'body.target_cluster_ids = targetClusterIds' in source
    assert "if (currentScope !== \"auto\")" in source
    assert 'id="cluster"' in dashboard
    assert "selectedCluster" in dashboard
    assert "context_cluster_id: root.dataset.contextCluster" not in source
    assert "body.target_cluster_ids = targetClusterIds" in source


def test_shiftlight_clarification_selection_is_a_bound_history_turn() -> None:
    source = shiftlight_source()
    assert "result.clarificationId" in source
    assert "!item.clarificationId" in source
    assert "clarificationId: item.clarificationId" in source
    assert 'select(choice.name, [choice.id])' in source
    assert 'select("HEPSİ"' in source
    assert "sendQuestion(item.pendingQuestion, ids, label" in source
    assert "clearBoundClarification(clarificationBinding)" in source
    assert "mcp_url" not in source.lower()


def test_shiftlight_evidence_fact_labels_and_allowlist_survive_ui_path() -> None:
    source = shiftlight_source()
    for key in (
        "resource_count", "degraded_true_count", "available_false_count",
        "progressing_true_count",
    ):
        assert key in source
    assert 'resource_count: "Toplam kaynak"' in source
    assert 'progressing_true_count: "Progressing"' in source
    assert '["kkbtest", "rmtest"].includes(item.cluster)' in source
    assert 'addText(list, "dt", "", "Cluster")' in source


def test_favicon_uses_existing_kbb_asset() -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_shiftlight_mascot_is_local_and_integrated_globally() -> None:
    static = Path(__file__).parents[1] / "app/static"
    mascot = static / "shiftlight-mascot.png"
    partial, source = shiftlight_partial(), shiftlight_source()
    css = (static / "shiftlight_assistant.css").read_text()
    assert mascot.is_file() and mascot.stat().st_size > 1000
    assert partial.count('/static/shiftlight-mascot.png') == 2
    assert 'avatar.src = "/static/shiftlight-mascot.png"' in source
    assert 'mascot.src = "/static/shiftlight-mascot.png"' in source
    assert "@keyframes shiftlight-flight" in css
    assert ".shiftlight-launcher-mascot" in css
    assert "width:62px; height:62px" in css
    assert "width:128px; height:128px" in css
    assert "width:132px; height:132px" in css
    assert "width:30px; height:30px" in css
    assert "animation:none" in css


def test_shiftlight_layout_stays_bounded_with_many_messages() -> None:
    source = shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert "MAX_MESSAGES = 100" in source
    assert "overflow-y:auto" in css
    assert "overflow-x:hidden" in css
    assert "nearConversationBottom" in source
    assert "previousScrollTop" in source
    assert "followBottom ? conversation.scrollHeight : previousScrollTop" in source
    assert ".shiftlight-empty { display:flex; flex:1 1 auto" in css
    assert "min-height:260px" not in css


def test_shiftlight_fullscreen_reuses_drawer_and_preserves_ui_state() -> None:
    partial, source = shiftlight_partial(), shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert 'id="shiftlight-expand"' in partial
    assert "ShiftLight'ı tam ekran aç" in partial
    assert 'drawer.classList.toggle("fullscreen", expanded)' in source
    assert 'drawer.setAttribute("aria-modal", String(expanded))' in source
    assert "const scrollTop = conversation.scrollTop" in source
    assert "conversation.scrollTop = scrollTop" in source
    assert "messageInput.value" not in source[source.index("const setFullscreen"):source.index("const openFullscreen")]
    assert "persistStore()" not in source[source.index("const setFullscreen"):source.index("const openFullscreen")]
    assert 'if (fullscreen) closeFullscreen(); else closeDrawer();' in source
    assert "width:min(1400px,96vw)" in css
    assert "height:94dvh" in css
    assert "height:100dvh" in css
    assert "body.shiftlight-fullscreen-open" in css


def test_shiftlight_reopened_conversation_remains_continuable_in_fullscreen() -> None:
    source = shiftlight_source()
    assert "isHistoricalConversation" not in source
    assert "form.hidden = false" in source
    assert "expirePendingClarification(item); store.activeConversationId = item.id" in source
    assert "messageInput.focus()" in source
    fullscreen_block = source[source.index("const setFullscreen"):source.index("const closeDrawer")]
    assert "activeConversationId" not in fullscreen_block
    assert "form.hidden" not in fullscreen_block


def test_typed_clarification_selection_reuses_exact_pending_question() -> None:
    source = shiftlight_source()
    assert '"kkbtest": "kkbtest"' in source
    assert '"kkb test": "kkbtest"' in source
    assert '"rm test": "rmtest"' in source
    assert '["hepsi", "tumu", "all"]' in source
    assert "pending.pendingQuestion, selectedIds, displayText" in source
    assert "conversationId: current.id, clarificationId: pending.clarificationId" in source
    assert "if (pending) { expirePendingClarification(current); persistStore(); }" in source


def test_shiftlight_turn_errors_persist_and_are_updated_by_turn_id() -> None:
    source = shiftlight_source()
    assert "const turnId = conversationId()" in source
    assert 'status: "pending"' in source
    assert "item.turnId === turn.turnId" in source
    assert 'settleTurn(turn, {text: message, evidence: [], status: "error"})' in source
    assert "current.messages.push(" in source
    assert "conversation.appendChild(shell.article)" not in source[source.index("} catch (error)"):source.index("} finally")]
    assert 'message.status === "pending"' in source
    assert "message.status = \"error\"" in source


def test_error_turns_and_composer_survive_history_drawer_and_fullscreen() -> None:
    source = shiftlight_source()
    partial = shiftlight_partial()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert "safeMessage({...assistantMessage" in source
    assert '["pending", "success", "error"].includes(item.status)' in source
    assert 'if (item.status === "error") shell.article.classList.add("error")' in source
    assert "form.hidden = false" in source
    assert '<form id="shiftlight-form" class="shiftlight-composer">' in partial
    assert ".shiftlight-drawer.open.fullscreen" in css
    assert ".shiftlight-composer { position:relative; bottom:auto; flex:0 0 auto" in css


def test_shiftlight_tables_have_independent_safe_csv_exports() -> None:
    source = shiftlight_source()
    css = (Path(__file__).parents[1] / "app/static/shiftlight_assistant.css").read_text()
    assert 'addText(actions, "button", "", "CSV İndir")' in source
    assert "addTableActions(wrapper, table)" in source
    assert 'download.addEventListener("click", () => downloadTableCsv(table))' in source
    assert "[...table.rows]" in source and "[...row.cells]" in source
    assert 'cell.textContent || ""' in source
    assert 'safe.replace(/"/g, \'""\')' in source
    assert '/[",\\n]/.test(safe)' in source
    assert '/^[\\t ]*[=+\\-@]/.test(safe)' in source
    assert "['\\uFEFF', csv]" not in source
    assert '["\\uFEFF", csv]' in source
    assert 'type: "text/csv;charset=utf-8"' in source
    assert "shiftlight-table-" in source
    assert "URL.revokeObjectURL(url)" in source
    assert "position:sticky" in css
    assert ".shiftlight-table-viewport{max-width:100%;overflow:auto}" in css
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source


def test_shiftlight_table_or_code_answer_can_open_detailed_view() -> None:
    source = shiftlight_source()
    assert 'shell.answer.querySelector("table, pre")' in source
    assert '"shiftlight-detail-action", "Detaylı Gör"' in source
    assert "openFullscreen(shell.article)" in source
    assert "focusTarget.scrollIntoView" in source
    assert "appendEvidence(shell.article, item.evidence)" in source
