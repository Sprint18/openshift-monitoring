from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.classification import classify_conversation
from app.main import create_app
from tests.test_ai_backend import settings
from tests.test_observations import operator


@pytest.mark.parametrize("message", [
    "benim adım alkın", "ben alkın", "teşekkür ederim", "sağol", "güzel",
    "tamam", "görüşürüz", "nasılsın", "çok güzel",
])
def test_short_messages_without_operational_signals_are_conversational(
    message: str,
) -> None:
    assert classify_conversation(message).conversation_class == "conversational"


@pytest.mark.parametrize("message", [
    "uat-zauh projesinde her şey yolunda mı",
    "pod durumunu kontrol et",
    "node CPU kullanımını göster",
    "namespace health status",
])
def test_resource_or_diagnostic_signals_remain_operational(message: str) -> None:
    assert classify_conversation(message).conversation_class == "operational"


@patch("app.main.MCPClient")
def test_conversational_request_with_all_scope_returns_once_without_mcp(
    mcp_class: Mock,
) -> None:
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat",
        json={"message": "merhaba", "conversation_scope": "all"},
    )
    assert response.status_code == 200
    assert response.json()["clusters"] == []
    assert response.json()["answer"].count("Merhaba") == 1
    mcp_class.assert_not_called()


def _generic_health_agent(message: str) -> tuple[AgentResult, Mock]:
    mcp, llm = Mock(), Mock()
    mcp.list_tools.return_value = [{
        "name": "resources_list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"}, "kind": {"type": "string"},
            },
        },
    }]
    llm.chat_completion.return_value = {
        "content": "Bu kaynak mevcut araçlarla kesin olarak sayılamadı."
    }
    result = AgentLoop(
        settings(token="token"), llm, mcp, "rmtest", "RMTEST"
    ).run(message)
    return result, mcp


@pytest.mark.parametrize("message", [
    "kaç tane sağlıksız pod var",
    "pod sağlığı",
    "deployment'lar sağlıklı mı",
    "namespace durumu nasıl",
])
def test_explicit_resource_signal_does_not_invoke_clusteroperator_prefetch(
    message: str,
) -> None:
    result, mcp = _generic_health_agent(message)
    assert "ClusterOperator" not in result.answer
    assert result.evidence == []
    mcp.call_tool.assert_not_called()


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_unhealthy_pod_query_resolves_rmtest_without_co_facts(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult(
        "Pod verisi tam olarak doğrulanamadı.", [], []
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat",
        json={"message": "rmtest ortamında kaç tane sağlıksız pod var"},
    )
    assert response.status_code == 200
    assert mcp_class.call_args.args[0] == "https://rm-mcp.example/mcp"
    assert "ClusterOperator" not in response.json()["answer"]
    assert response.json()["evidence"] == []


def test_strict_clusteroperator_fast_path_is_preserved() -> None:
    mcp, llm = Mock(), Mock()
    mcp.list_tools.return_value = [{
        "name": "resources_list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"}, "kind": {"type": "string"},
            },
            "required": ["apiVersion", "kind"],
        },
    }]
    mcp.call_tool.return_value = {"items": [operator("authentication")]}
    result = AgentLoop(
        settings(token=None), llm, mcp, "kkbtest", "KKB TEST"
    ).run("degraded co var mı")
    assert "Toplam ClusterOperator: **1**" in result.answer
    assert result.evidence[0]["facts"]["degraded_true_count"] == 0
    llm.chat_completion.assert_not_called()
