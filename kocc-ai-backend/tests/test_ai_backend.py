from __future__ import annotations

import json
import urllib.error
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.clusters import (
    ClusterScope, UnknownClusterError, cluster_registry,
    explicit_cluster_scope, resolve_cluster_request, selected_cluster,
)
from app.agent import AgentResult
from app.config import Settings, load_settings
from app.llm_client import LLMClient, LLMUnavailable
from app.main import create_app
from app.mcp_client import MCPClient, MCPUnavailable, parse_mcp_body
from tests.test_observations import operator


def settings(token: str | None = None) -> Settings:
    return Settings(
        llm_base_url="https://llm.example",
        llm_api_token=token,
        llm_model="test-model",
        llm_timeout_seconds=2,
        mcp_kkbtest_url="http://mcp.example/mcp",
        mcp_rmtest_url="https://rm-mcp.example/mcp",
        mcp_timeout_seconds=2,
    )


def test_configuration_defaults_and_missing_token(monkeypatch) -> None:
    for name in (
        "AI_LLM_BASE_URL", "AI_LLM_API_TOKEN", "AI_LLM_MODEL",
        "AI_LLM_TIMEOUT_SECONDS", "AI_MCP_KKBTEST_URL", "AI_MCP_RMTEST_URL",
        "AI_MCP_TIMEOUT_SECONDS", "AI_AGENT_MAX_ITERATIONS",
        "AI_AGENT_MAX_TOOL_CALLS", "AI_AGENT_MAX_TOOL_RESULT_CHARS",
        "AI_AGENT_MAX_USER_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    configured = load_settings()
    assert configured.llm_base_url == "https://llm.kkb.com.tr"
    assert configured.llm_api_token is None
    assert configured.mcp_kkbtest_url == "http://openshift-mcp:8080/mcp"
    assert configured.mcp_rmtest_url == "https://mcp.apps.rmocptest1.kkbdomain.com/mcp"
    assert configured.agent_max_iterations == 6
    assert configured.agent_max_tool_calls == 10
    with pytest.raises(LLMUnavailable):
        LLMClient(configured).check()


def test_configuration_rejects_invalid_timeout(monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="AI_MCP_TIMEOUT_SECONDS"):
        load_settings()


def test_agent_configuration_limits(monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT_MAX_ITERATIONS", "10")
    monkeypatch.setenv("AI_AGENT_MAX_TOOL_CALLS", "12")
    monkeypatch.setenv("AI_AGENT_MAX_TOOL_RESULT_CHARS", "50000")
    monkeypatch.setenv("AI_AGENT_MAX_USER_CHARS", "9000")
    configured = load_settings()
    assert configured.agent_max_iterations == 10
    assert configured.agent_max_tool_calls == 12
    assert configured.agent_max_tool_result_chars == 50000
    assert configured.agent_max_user_chars == 9000

    monkeypatch.setenv("AI_AGENT_MAX_ITERATIONS", "11")
    with pytest.raises(ValueError, match="AI_AGENT_MAX_ITERATIONS"):
        load_settings()


def test_cluster_registry_exposes_only_public_fields() -> None:
    registry = cluster_registry(settings())
    assert list(registry) == ["kkbtest", "rmtest"]
    assert registry["kkbtest"].public_dict() == {
        "id": "kkbtest", "name": "KKB TEST", "enabled": True,
    }
    assert "mcp_url" not in registry["kkbtest"].public_dict()
    assert registry["rmtest"].public_dict() == {
        "id": "rmtest", "name": "RMTEST", "enabled": True,
    }
    assert "mcp_url" not in registry["rmtest"].public_dict()
    with pytest.raises(UnknownClusterError):
        selected_cluster(registry, "unknown")


def test_health_and_cluster_endpoint_do_not_require_dependencies() -> None:
    client = TestClient(create_app(settings()))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/v1/clusters").json() == {
        "clusters": [
            {"id": "kkbtest", "name": "KKB TEST", "enabled": True},
            {"id": "rmtest", "name": "RMTEST", "enabled": True},
        ]
    }


@pytest.mark.parametrize(("message", "expected"), [
    ("KKBTEST nasıl?", "kkbtest"),
    ("kkb test node durumuna bak", "kkbtest"),
    ("RMTEST'e bak", "rmtest"),
    ("RMTEST'te degraded operator var mı?", "rmtest"),
    ("RMTEST'in durumunu kontrol et", "rmtest"),
    ("rm test clusterında degraded operator var mı?", "rmtest"),
    ("rmocptest1 durumunu kontrol et", "rmtest"),
])
def test_explicit_cluster_alias_resolution(message: str, expected: str) -> None:
    scope = explicit_cluster_scope(message, cluster_registry(settings()))
    assert scope == ClusterScope("single", (expected,))


@pytest.mark.parametrize(("message", "expected_cluster", "operational"), [
    ("rmtestteki node cpu ve memory kullanımı nedir", "rmtest",
     "node cpu ve memory kullanımı nedir"),
    ("rmocptest1 clusterındaki node cpu ve memory kullanımı nasıl", "rmtest",
     "node cpu ve memory kullanımı nasıl"),
    ("kkbtestteki node cpu ve memory kullanımı nasıl", "kkbtest",
     "node cpu ve memory kullanımı nasıl"),
])
def test_runtime_alias_is_removed_from_operational_question(
    message: str, expected_cluster: str, operational: str,
) -> None:
    resolved = resolve_cluster_request(message, cluster_registry(settings()))
    assert resolved is not None
    assert resolved.scope == ClusterScope("single", (expected_cluster,))
    assert resolved.operational_message == operational


@pytest.mark.parametrize("message", [
    "tüm clusterlara bak", "bütün clusterları kontrol et",
    "hepsinin sağlık durumunu kontrol et", "all clusters",
    "check all clusters",
])
def test_all_cluster_intent_is_registry_owned(message: str) -> None:
    scope = explicit_cluster_scope(message, cluster_registry(settings()))
    assert scope == ClusterScope("all", ("kkbtest", "rmtest"))


def test_unknown_and_conflicting_alias_resolution_is_conservative() -> None:
    registry = cluster_registry(settings())
    assert explicit_cluster_scope("unknown cluster'a bak", registry) is None
    assert explicit_cluster_scope("KKBTEST ve RMTEST'i karşılaştır", registry) == (
        ClusterScope("multiple", ("kkbtest", "rmtest"))
    )


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
        "cluster": "unknown", "message": "test",
    })
    assert unknown.status_code == 404


@patch("app.main.MCPClient")
@patch("app.llm_client.urllib.request.urlopen")
def test_llm_network_error_is_controlled(
    urlopen: Mock, mcp_class: Mock
) -> None:
    mcp_class.return_value.list_tools.return_value = []
    urlopen.side_effect = urllib.error.URLError("internal network detail")
    client = TestClient(create_app(settings(token="not-a-real-token")))
    response = client.post("/api/v1/chat", json={
        "cluster": "kkbtest", "message": "durumu kontrol et",
    })
    assert response.status_code == 503
    assert response.json() == {"error": "llm_unavailable"}
    assert "internal network detail" not in response.text


@patch("app.main.MCPClient")
def test_message_size_limit_is_controlled(mcp_class: Mock) -> None:
    limited = settings(token="token")
    limited = Settings(**{
        **limited.__dict__, "agent_max_user_chars": 5,
    })
    response = TestClient(create_app(limited)).post("/api/v1/chat", json={
        "cluster": "kkbtest", "message": "too long",
    })
    assert response.status_code == 400
    assert response.json() == {"error": "message_too_large"}
    mcp_class.assert_not_called()


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_chat_response_keeps_contract_and_adds_success_evidence(
    mcp_class: Mock, agent_class: Mock
) -> None:
    agent_class.return_value.run.return_value = AgentResult(
        answer="Grounded answer",
        tool_calls=[
            {"name": "resources_list", "status": "success"},
            {"name": "nodes_top", "status": "error"},
        ],
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat",
        json={"cluster": "kkbtest", "message": "health"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "cluster": "kkbtest",
        "answer": "Grounded answer",
        "tool_calls": [
            {"name": "resources_list", "status": "success"},
            {"name": "nodes_top", "status": "error"},
        ],
        "evidence": [
            {"cluster": "kkbtest", "tool": "resources_list", "status": "success"}
        ],
    }


@pytest.mark.parametrize(("legacy", "message", "expected_url", "expected_cluster"), [
    ("rmtest", "KKBTEST'teki node CPU durumuna bak", "http://mcp.example/mcp", "kkbtest"),
    ("kkbtest", "RMTEST'teki node CPU durumuna bak", "https://rm-mcp.example/mcp", "rmtest"),
])
@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_cluster_text_routes_only_to_registry_owned_mcp(
    mcp_class: Mock, agent_class: Mock, legacy: str, message: str,
    expected_url: str, expected_cluster: str,
) -> None:
    agent_class.return_value.run.return_value = AgentResult(
        "Grounded", [{"name": "resources_list", "status": "success"}]
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={"cluster": legacy, "message": message},
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == expected_cluster
    assert response.json()["evidence"] == [{
        "cluster": expected_cluster,
        "tool": "resources_list", "status": "success",
    }]
    mcp_class.assert_called_once_with(expected_url, 2)
    agent_class.return_value.run.assert_called_once_with("node CPU durumuna bak")
    assert agent_class.call_args.args[3] == expected_cluster


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_all_clusters_fans_out_with_cluster_scoped_facts(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.side_effect = [
        AgentResult("KKB grounded", [], [{
            "tool": "resources_list", "status": "success",
            "facts": {"resource_count": 34},
        }]),
        AgentResult("RM grounded", [], [{
            "tool": "resources_list", "status": "success",
            "facts": {"resource_count": 35},
        }]),
    ]
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat",
        json={"cluster": "kkbtest", "message": "tüm clusterlara bak"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["cluster"] == "all"
    assert "## KKB TEST\n\nKKB grounded" in payload["answer"]
    assert "## RMTEST\n\nRM grounded" in payload["answer"]
    assert payload["evidence"] == [
        {"cluster": "kkbtest", "tool": "resources_list", "status": "success",
         "facts": {"resource_count": 34}},
        {"cluster": "rmtest", "tool": "resources_list", "status": "success",
         "facts": {"resource_count": 35}},
    ]
    assert [call.args[0] for call in mcp_class.call_args_list] == [
        "http://mcp.example/mcp", "https://rm-mcp.example/mcp",
    ]
    assert "mcp.example" not in response.text


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_all_clusters_preserves_success_when_remote_cluster_fails(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.side_effect = [
        AgentResult("KKB grounded", [], [{
            "tool": "resources_list", "status": "success",
            "facts": {"resource_count": 34},
        }]),
        MCPUnavailable("private remote detail"),
    ]
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat",
        json={"cluster": "kkbtest", "message": "check all clusters"},
    )
    assert response.status_code == 200
    assert "## KKB TEST\n\nKKB grounded" in response.json()["answer"]
    assert "## RMTEST\n\nCluster verisi şu anda kullanılamıyor." in response.json()["answer"]
    assert response.json()["evidence"] == [{
        "cluster": "kkbtest", "tool": "resources_list", "status": "success",
        "facts": {"resource_count": 34},
    }]
    assert "private remote detail" not in response.text


def cluster_operator_mcp(count: int) -> Mock:
    client = Mock()
    client.list_tools.return_value = [{
        "name": "resources_list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["apiVersion", "kind"],
        },
    }]
    client.call_tool.return_value = {
        "items": [operator(f"co-{index}") for index in range(count)]
    }
    return client


@patch("app.main.MCPClient")
def test_all_cluster_operator_query_uses_separate_authoritative_facts(
    mcp_class: Mock,
) -> None:
    mcp_class.side_effect = [cluster_operator_mcp(33), cluster_operator_mcp(34)]
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "Bütün clusterlara bak, degraded ClusterOperator var mı?"
        },
    )
    assert response.status_code == 200
    answer = response.json()["answer"]
    assert "## KKB TEST\n\n- Toplam ClusterOperator: **33**" in answer
    assert "## RMTEST\n\n- Toplam ClusterOperator: **34**" in answer
    assert [item["cluster"] for item in response.json()["evidence"]] == [
        "kkbtest", "rmtest",
    ]


@patch("app.main.MCPClient")
def test_all_cluster_operator_query_preserves_local_facts_on_remote_failure(
    mcp_class: Mock,
) -> None:
    local = cluster_operator_mcp(33)
    remote = cluster_operator_mcp(34)
    remote.call_tool.side_effect = MCPUnavailable("private failure")
    mcp_class.side_effect = [local, remote]
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "Bütün clusterlara bak, degraded ClusterOperator var mı?"
        },
    )
    assert response.status_code == 200
    assert "## KKB TEST\n\n- Toplam ClusterOperator: **33**" in response.json()["answer"]
    assert "## RMTEST\n\nClusterOperator durumu bu sorguda doğrudan doğrulanamadı." in response.json()["answer"]
    assert "private failure" not in response.text
    assert [item["cluster"] for item in response.json()["evidence"]] == ["kkbtest"]


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_ai_chat_operational_log_contains_only_safe_metadata(
    mcp_class: Mock, agent_class: Mock, caplog
) -> None:
    secret_prompt = "private-user-question-never-log"
    agent_class.return_value.run.return_value = AgentResult(
        answer="Grounded",
        tool_calls=[{"name": "resources_list", "status": "success"}],
        evidence_items=[{
            "tool": "resources_list", "status": "success",
            "facts": {"resource_count": 34},
        }],
        iterations=1,
    )
    with caplog.at_level("INFO", logger="kocc_ai"):
        response = TestClient(create_app(settings(token="secret-token"))).post(
            "/api/v1/chat",
            json={"cluster": "kkbtest", "message": secret_prompt},
        )
    assert response.status_code == 200
    assert "ai_chat_route scope=single target_cluster=kkbtest" in caplog.text
    assert "ai_chat_complete cluster=kkbtest outcome=success" in caplog.text
    assert "tools=resources_list:success" in caplog.text
    assert "facts=True iterations=1" in caplog.text
    assert secret_prompt not in caplog.text
    assert "secret-token" not in caplog.text
    assert "mcp.example" not in caplog.text


def test_sse_response_parser() -> None:
    result = parse_mcp_body(
        "text/event-stream",
        b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n",
    )
    assert result["result"] == {}


@pytest.mark.parametrize("response", [
    {},
    {"choices": []},
    {"choices": [{}]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {"content": None, "tool_calls": None}}]},
    {"choices": [{"message": {"content": 42}}]},
    {"choices": [{"message": {"content": None, "tool_calls": {}}}]},
])
def test_llm_malformed_responses_are_controlled(response: dict) -> None:
    client = LLMClient(settings(token="token"))
    client._request = Mock(return_value=response)
    with pytest.raises(LLMUnavailable):
        client.chat_completion([{"role": "user", "content": "test"}])


def test_llm_tool_call_payload_uses_openai_compatible_fields() -> None:
    client = LLMClient(settings(token="token"))
    client._request = Mock(return_value={
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "one", "function": {
                "name": "resources_list", "arguments": "{}",
            }}],
        }}],
    })
    tools = [{"type": "function", "function": {
        "name": "resources_list", "parameters": {"type": "object"},
    }}]
    result = client.chat_completion(
        [{"role": "user", "content": "test"}], tools, "auto"
    )
    assert result["tool_calls"][0]["id"] == "one"
    payload = client._request.call_args.args[1]
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"
    assert "temperature" not in payload


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
