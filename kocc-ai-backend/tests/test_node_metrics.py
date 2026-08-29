from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentLoop
from app.main import create_app
from app.mcp_client import MCPUnavailable
from app.node_metrics import parse_node_metrics, render_node_metrics
from tests.test_ai_backend import settings


def _tool() -> dict:
    return {
        "name": "nodes_top",
        "inputSchema": {"type": "object", "properties": {}},
    }


def _result(node: str, cpu: str = "120m") -> dict:
    return {"nodes": [{
        "name": node, "cpu": cpu, "cpuPercent": "6%",
        "memory": "1024Mi", "memoryPercent": "25%",
    }]}


@pytest.mark.parametrize(("cluster_id", "cluster_name"), [
    ("kkbtest", "KKB TEST"), ("rmtest", "RMTEST"),
])
def test_direct_node_metrics_are_deterministic_without_llm(
    cluster_id: str, cluster_name: str,
) -> None:
    mcp, llm = Mock(), Mock()
    mcp.list_tools.return_value = [_tool()]
    mcp.call_tool.return_value = _result("worker-1")
    llm.chat_completion.side_effect = AssertionError("LLM must not be called")
    result = AgentLoop(
        settings(token=None), llm, mcp, cluster_id, cluster_name
    ).run("node CPU ve memory kullanımı")
    assert "Node CPU ve Memory Kullanımı" in result.answer
    assert "| worker-1 | 120m | 6% | 1024Mi | 25% |" in result.answer
    llm.chat_completion.assert_not_called()


def test_node_metrics_table_parser_and_parse_failure() -> None:
    facts = parse_node_metrics({"content": [{"type": "text", "text": (
        "NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%\n"
        "worker-a 250m 12% 2048Mi 50%\n"
    )}]}, "kkbtest")
    assert facts is not None
    assert facts.nodes[0].cpu_millicores == 250
    assert "worker-a" in render_node_metrics(facts)
    assert parse_node_metrics({"nodes": [{"name": "worker-a"}]}) is None


def _mcp(node: str, failure: bool = False) -> Mock:
    client = Mock()
    client.list_tools.return_value = [_tool()]
    client.call_tool.side_effect = (
        MCPUnavailable("private") if failure else None
    )
    if not failure:
        client.call_tool.return_value = _result(node)
    return client


@patch("app.main.MCPClient")
def test_multi_cluster_node_metrics_are_independent_and_partial_safe(
    mcp_class: Mock,
) -> None:
    mcp_class.side_effect = [_mcp("kkb-worker"), _mcp("rm-worker", failure=True)]
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": "KKBTEST ve RMTEST node CPU kullanımını karşılaştır"
        },
    )
    assert response.status_code == 200
    assert "## KKB TEST" in response.json()["answer"]
    assert "kkb-worker" in response.json()["answer"]
    assert "## RMTEST" in response.json()["answer"]
    assert "doğrulanamadı" in response.json()["answer"]


@patch("app.main.MCPClient")
def test_multi_cluster_node_metrics_both_render_without_llm(
    mcp_class: Mock,
) -> None:
    mcp_class.side_effect = [_mcp("kkb-worker"), _mcp("rm-worker")]
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": "KKBTEST ve RMTEST node memory kullanımını karşılaştır"
        },
    )
    assert response.status_code == 200
    assert "## KKB TEST" in response.json()["answer"]
    assert "kkb-worker" in response.json()["answer"]
    assert "## RMTEST" in response.json()["answer"]
    assert "rm-worker" in response.json()["answer"]
