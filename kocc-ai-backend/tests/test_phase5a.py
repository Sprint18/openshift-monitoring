from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.classification import classify_conversation
from app.evidence import EvidenceEnvelope, EvidenceResource
from app.main import create_app
from tests.test_ai_backend import settings
from tests.test_observations import operator


@pytest.mark.parametrize(("message", "subtype"), [
    ("merhaba", "greeting"),
    ("nasılsın shift", "greeting"),
    ("openshift dışında merhaba", "greeting"),
    ("sen kimsin", "identity"),
    ("ne yapabilirsin", "help"),
    ("nasıl çalışıyorsun", "help"),
])
def test_conversational_families_bypass_scope_and_mcp(
    message: str, subtype: str,
) -> None:
    classification = classify_conversation(message)
    assert classification.conversation_class == "conversational"
    assert classification.subtype == subtype
    with patch("app.main.MCPClient") as mcp_class:
        response = TestClient(create_app(settings(token=None))).post(
            "/api/v1/chat", json={
                "message": message, "conversation_scope": "rmtest",
            },
        )
    assert response.status_code == 200
    assert response.json()["clusters"] == []
    mcp_class.assert_not_called()


def test_uncertain_operational_request_remains_clarification() -> None:
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={"message": "degraded co var mı"},
    )
    assert response.status_code == 200
    assert response.json()["needs_cluster_selection"] is True


def test_greeting_with_operational_request_falls_through_to_routing() -> None:
    classification = classify_conversation("merhaba node CPU durumunu göster")
    assert classification.conversation_class == "operational"


@pytest.mark.parametrize(("payload", "expected_endpoint"), [
    ({"message": "KKBTEST degraded co var mı", "conversation_scope": "rmtest"},
     "http://mcp.example/mcp"),
    ({"message": "RMTEST node CPU", "conversation_scope": "kkbtest"},
     "https://rm-mcp.example/mcp"),
    ({"message": "node CPU", "target_cluster_ids": ["rmtest"],
      "conversation_scope": "kkbtest"}, "https://rm-mcp.example/mcp"),
])
@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_scope_precedence_is_backend_authoritative(
    mcp_class: Mock, agent_class: Mock, payload: dict, expected_endpoint: str,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("verified", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json=payload,
    )
    assert response.status_code == 200
    assert mcp_class.call_args.args[0] == expected_endpoint


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_multi_cluster_request_targets_both(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("verified", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "KKBTEST ve RMTEST node durumunu karşılaştır",
            "conversation_scope": "kkbtest",
        },
    )
    assert response.status_code == 200
    assert [call.args[0] for call in mcp_class.call_args_list] == [
        "http://mcp.example/mcp", "https://rm-mcp.example/mcp",
    ]


@pytest.mark.parametrize("completeness", ["complete", "partial", "unavailable"])
def test_evidence_envelope_completeness_and_safe_legacy_projection(
    completeness: str,
) -> None:
    envelope = EvidenceEnvelope.create(
        cluster_id="kkbtest",
        operation="list",
        resource=EvidenceResource(
            api_version="config.openshift.io/v1", kind="ClusterOperator"
        ),
        completeness=completeness,
        facts={"resource_count": 34, "raw_mcp_payload": {"secret": "hidden"}},
        provenance={"tool": "resources_list"},
    )
    assert envelope.completeness == completeness
    metadata = envelope.legacy_metadata()
    assert metadata["status"] == (
        "error" if completeness == "unavailable" else "success"
    )
    assert metadata.get("facts") == {"resource_count": 34}
    assert "raw_mcp_payload" not in str(metadata)


def test_node_fast_path_uses_canonical_evidence_without_llm_or_raw_payload() -> None:
    mcp, llm = Mock(), Mock()
    mcp.list_tools.return_value = [{
        "name": "nodes_top", "inputSchema": {"type": "object", "properties": {}},
    }]
    mcp.call_tool.return_value = {"nodes": [{
        "name": "worker-1", "cpu": "100m", "memory": "1Gi",
    }]}
    result = AgentLoop(
        settings(token=None), llm, mcp, "kkbtest", "KKB TEST"
    ).run("node CPU")
    assert isinstance(result.evidence_items[0], EvidenceEnvelope)
    assert result.evidence_items[0].facts["node_count"] == 1
    assert result.evidence == [{"tool": "nodes_top", "status": "success"}]
    llm.chat_completion.assert_not_called()


def test_cluster_operator_fast_path_uses_complete_canonical_evidence() -> None:
    mcp, llm = Mock(), Mock()
    mcp.list_tools.return_value = [{
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
    mcp.call_tool.return_value = {
        "items": [operator(f"co-{index}") for index in range(3)]
    }
    result = AgentLoop(
        settings(token=None), llm, mcp, "kkbtest", "KKB TEST"
    ).run("degraded co var mı")
    envelope = result.evidence_items[0]
    assert isinstance(envelope, EvidenceEnvelope)
    assert envelope.completeness == "complete"
    assert envelope.resource.kind == "ClusterOperator"
    assert envelope.facts["resource_count"] == 3
    assert result.evidence[0]["facts"]["degraded_true_count"] == 0
    llm.chat_completion.assert_not_called()
