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
    assert "AI Assistant" in response.text
    assert "yalnızca read-only cluster işlemlerini destekler" in response.text
    assert 'aria-label="Dashboard navigation"' in response.text
    assert 'aria-current="page"' in response.text


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
        "clusters": [{"id": "kkbtest", "name": "KKB TEST", "enabled": True}]
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
    ai_client.chat.assert_called_once_with("kkbtest", "Cluster health")


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
def test_ai_chat_rejects_unsupported_cluster(ai_client: Mock) -> None:
    response = client.post("/api/ai/chat", json={
        "cluster": "rmtest", "message": "health",
    })
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_cluster"
    ai_client.chat.assert_not_called()


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
        "answer": "Safe answer",
        "tool_calls": [{"name": "nodes_top", "status": "success"}],
        "evidence": [{"tool": "nodes_top", "status": "success"}],
    }


def test_ai_template_uses_safe_text_rendering_and_only_kocc_endpoints() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert "innerHTML" not in source
    assert "textContent" in source
    assert 'fetchKoccJson("/api/ai/clusters")' in source
    assert 'fetchKoccJson("/api/ai/chat"' in source
    assert "svc.cluster.local" not in source
    assert "data.evidence" in source
    assert "item.tool" in source


def test_ai_template_checks_status_and_content_type_before_json() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert 'response.headers.get("Content-Type")' in source
    assert 'contentType.startsWith("application/json")' in source
    assert "if (!response.ok)" in source
    assert "try {\n        return await response.json();" in source
    assert "return null;" in source
    assert "response.text()" not in source
    assert "error.message" not in source


def test_ai_template_maps_http_and_network_errors_to_safe_messages() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert 'status === 400) return "request"' in source
    assert 'status === 502 || status === 503) return "unavailable"' in source
    assert 'status === 504) return "timeout"' in source
    assert "İstek AI Assistant tarafından işlenemedi." in source
    assert "AI Assistant şu anda kullanılamıyor. Lütfen daha sonra tekrar deneyin." in source
    assert "AI Assistant yanıtı zaman aşımına uğradı. Lütfen tekrar deneyin." in source
    assert "AI Assistant isteği tamamlanamadı." in source
    assert 'error.name === "AbortError" ? "timeout" : "unavailable"' in source
    assert "Unexpected token" not in source
    assert "Gateway Timeout" not in source
    assert "<html>" not in source


def test_ai_template_preserves_loading_deduplication_and_evidence() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert "if (requestPending) return;" in source
    assert "setPending(true);" in source
    assert "sendButton.disabled = pending" in source
    assert "data.evidence.forEach" in source
    assert 'item.status !== "success"' in source


def test_ai_template_is_a_scrollable_chat_workspace() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert 'class="chat-workspace"' in source
    assert ".chat-workspace {" in source
    assert "grid-template-rows:auto minmax(0,1fr) auto" in source
    assert "overflow-y:auto" in source
    assert "conversation.scrollHeight" in source
    assert "conversation.scrollTop" in source
    assert "conversation.clientHeight < 80" in source
    assert "if (nearBottom)" in source


def test_ai_template_keeps_composer_and_keyboard_behavior() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert '<form id="ai-form" class="composer">' in source
    assert '<textarea id="ai-message"' in source
    assert 'event.key === "Enter" && !event.shiftKey' in source
    assert "event.preventDefault();" in source
    assert "form.requestSubmit();" in source
    assert "!message.trim()" in source


def test_ai_template_new_chat_and_cluster_change_isolate_history() -> None:
    source = (
        Path(__file__).parents[1] / "app/templates/ai_assistant.html"
    ).read_text()
    assert 'id="new-chat"' in source
    assert "conversation.replaceChildren();" in source
    assert 'newChatButton.addEventListener("click"' in source
    assert 'clusterSelect.addEventListener("change"' in source
    assert "Cluster değiştirildi. Yeni sohbet başlatıldı." in source
    assert "newChatButton.disabled = pending" in source
    assert "exampleButtons.forEach" in source
