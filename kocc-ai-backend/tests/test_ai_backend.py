from __future__ import annotations

import json
import urllib.error
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.clusters import cluster_registry, selected_cluster, UnknownClusterError
from app.config import Settings, load_settings
from app.llm_client import LLMClient, LLMUnavailable
from app.main import create_app
from app.mcp_client import MCPClient, MCPUnavailable, parse_mcp_body


def settings(token: str | None = None) -> Settings:
    return Settings(
        llm_base_url="https://llm.example",
        llm_api_token=token,
        llm_model="test-model",
        llm_timeout_seconds=2,
        mcp_kkbtest_url="http://mcp.example/mcp",
        mcp_timeout_seconds=2,
    )


def test_configuration_defaults_and_missing_token(monkeypatch) -> None:
    for name in (
        "AI_LLM_BASE_URL", "AI_LLM_API_TOKEN", "AI_LLM_MODEL",
        "AI_LLM_TIMEOUT_SECONDS", "AI_MCP_KKBTEST_URL",
        "AI_MCP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    configured = load_settings()
    assert configured.llm_base_url == "https://llm.kkb.com.tr"
    assert configured.llm_api_token is None
    assert configured.mcp_kkbtest_url == "http://openshift-mcp:8080/mcp"
    with pytest.raises(LLMUnavailable):
        LLMClient(configured).check()


def test_configuration_rejects_invalid_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="AI_MCP_TIMEOUT_SECONDS"):
        load_settings()


def test_cluster_registry_exposes_only_public_fields() -> None:
    registry = cluster_registry(settings())
    assert list(registry) == ["kkbtest"]
    assert registry["kkbtest"].public_dict() == {
        "id": "kkbtest", "name": "KKB TEST", "enabled": True,
    }
    assert "mcp_url" not in registry["kkbtest"].public_dict()
    with pytest.raises(UnknownClusterError):
        selected_cluster(registry, "rmtest")


def test_health_and_cluster_endpoint_do_not_require_dependencies() -> None:
    client = TestClient(create_app(settings()))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/clusters").json() == {
        "clusters": [{"id": "kkbtest", "name": "KKB TEST", "enabled": True}]
    }


@patch("app.main.MCPClient")
def test_readiness_reports_degraded_without_crashing(mcp_class: Mock) -> None:
    mcp_class.return_value.list_tools.side_effect = MCPUnavailable("down")
    application = create_app(settings())
    application.state.llm_client = Mock()
    application.state.llm_client.check.side_effect = LLMUnavailable("down")
    response = TestClient(application).get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "dependencies": {"mcp": "unavailable", "llm": "unavailable"},
    }


@patch("app.main.MCPClient")
def test_mcp_unavailable_is_controlled(mcp_class: Mock) -> None:
    mcp_class.return_value.list_tools.side_effect = MCPUnavailable("internal detail")
    response = TestClient(create_app(settings())).get("/api/v1/mcp/status")
    assert response.status_code == 503
    assert response.json() == {"cluster": "kkbtest", "status": "unavailable"}
    assert "internal detail" not in response.text


def test_chat_missing_token_and_cluster_routing() -> None:
    client = TestClient(create_app(settings(token=None)))
    unavailable = client.post("/api/v1/chat", json={
        "cluster": "kkbtest", "message": "operator durumunu kontrol et",
    })
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "llm_unavailable"}
    unknown = client.post("/api/v1/chat", json={
        "cluster": "rmtest", "message": "test",
    })
    assert unknown.status_code == 404


@patch("app.llm_client.urllib.request.urlopen")
def test_llm_network_error_is_controlled(urlopen: Mock) -> None:
    urlopen.side_effect = urllib.error.URLError("internal network detail")
    client = TestClient(create_app(settings(token="not-a-real-token")))
    response = client.post("/api/v1/chat", json={
        "cluster": "kkbtest", "message": "durumu kontrol et",
    })
    assert response.status_code == 503
    assert response.json() == {"error": "llm_unavailable"}
    assert "internal network detail" not in response.text


def test_sse_response_parser() -> None:
    result = parse_mcp_body(
        "text/event-stream",
        b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n",
    )
    assert result["result"] == {}


class FakeResponse:
    def __init__(self, body: dict | None, session: str | None = None) -> None:
        self.body = b"" if body is None else json.dumps(body).encode()
        self.headers = {
            "Content-Type": "application/json",
            **({"Mcp-Session-Id": session} if session else {}),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


@patch("app.mcp_client.urllib.request.urlopen")
def test_mcp_initialize_session_and_tools_list(urlopen: Mock) -> None:
    urlopen.side_effect = [
        FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {}}, "session-1"),
        FakeResponse(None),
        FakeResponse({
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": [{"name": "pods_list"}]},
        }),
    ]
    client = MCPClient("http://mcp.example/mcp", timeout=2)
    tools = client.list_tools()
    assert tools == [{"name": "pods_list"}]
    assert client.session_id == "session-1"
    notification_request = urlopen.call_args_list[1].args[0]
    tools_request = urlopen.call_args_list[2].args[0]
    assert notification_request.get_header("Mcp-session-id") == "session-1"
    assert tools_request.get_header("Mcp-session-id") == "session-1"
