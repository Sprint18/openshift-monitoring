from __future__ import annotations

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent import AgentResult
from app.conversation import ActiveInspection, ConversationContext
from app.evidence import EvidenceEnvelope, EvidenceResource
from app.main import create_app
from app.observations import deterministic_observation
from tests.test_ai_backend import settings


def pod_inspection() -> ActiveInspection:
    return ActiveInspection(
        inspection_type="pod_health", resource_kind="Pod",
        cluster_id="kkbtest", namespace="uat-zauh",
        pod_count=2, ready_count=0, non_ready_count=2,
        total_restarts=252, max_restart_count=237,
        problematic_pod_names=("pod-a", "pod-b"),
        observed_at="2026-09-03T10:00:00+00:00",
    )


def namespace_context(*, inspection: bool = False) -> ConversationContext:
    return ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_filter_type="prefix", last_filter_value="uat",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
        active_inspection=pod_inspection() if inspection else None,
    )


def pod_result() -> AgentResult:
    evidence = EvidenceEnvelope.create(
        cluster_id="kkbtest", operation="inspect",
        resource=EvidenceResource(
            api_version="v1", kind="Pod", namespace="uat-zauh",
        ),
        completeness="partial",
        facts={
            "pod_count": 2, "ready_count": 0, "non_ready_count": 2,
            "total_restarts": 252, "max_restart_count": 237,
            "problematic_pod_names": ["pod-a", "pod-b"],
        },
        provenance={"tool": "pods_list"},
    )
    return AgentResult("pod tablosu", [], [evidence])


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_pod_inspection_is_recorded_and_followup_uses_canonical_facts(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = pod_result()
    client = TestClient(create_app(settings(token="token")))
    pods = client.post("/api/v1/chat", json={
        "message": "peki bunun podları nasıl",
        "conversation_context": namespace_context().public_dict(),
    })
    inspection_context = pods.json()["conversation_context"]
    assert inspection_context["active_inspection"]["inspection_type"] == "pod_health"
    assert inspection_context["active_inspection"]["non_ready_count"] == 2
    assert "raw" not in str(inspection_context)

    followup = client.post("/api/v1/chat", json={
        "message": "bunda bi sorun görüyor musun",
        "conversation_context": inspection_context,
    })
    assert "2/2 pod tam Ready değil" in followup.json()["answer"]
    assert "252 restart" in followup.json()["answer"]
    assert "237" in followup.json()["answer"]
    assert "kök nedeni göstermiyor" in followup.json()["answer"]
    assert agent_class.return_value.run.call_count == 1


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_cluster_scope_overrides_namespace_and_pod_inspection(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("multi-signal health", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "cluster genel olarak nasıl",
            "conversation_context": namespace_context(inspection=True).public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["answer"].endswith("multi-signal health")
    assert "namespace'ini mi" not in response.text
    assert agent_class.return_value.run.call_args.args[0] == "cluster genel olarak nasıl"


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_node_scope_overrides_namespace_entity(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("node inspection", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "node'lar nasıl",
            "conversation_context": namespace_context().public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["answer"].endswith("node inspection")
    assert "namespace'ini mi" not in response.text


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_casual_turn_preserves_entity_for_later_pod_inspection(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    application.state.llm_client.is_configured.return_value = True
    application.state.llm_client.chat_completion.return_value = {
        "content": "Rica ederim."
    }
    client = TestClient(application)
    casual = client.post("/api/v1/chat", json={
        "message": "teşekkürler",
        "conversation_context": namespace_context().public_dict(),
    })
    assert casual.json()["conversation_context"]["active_entity_name"] == "uat-zauh"

    agent_class.return_value.run.return_value = pod_result()
    pods = client.post("/api/v1/chat", json={
        "message": "peki bunun podları nasıl",
        "conversation_context": casual.json()["conversation_context"],
    })
    assert pods.status_code == 200
    assert "uat-zauh namespace" in agent_class.return_value.run.call_args.args[0]


def test_pod_observation_contains_only_bounded_canonical_facts() -> None:
    result = {"items": [
        {
            "metadata": {"name": "pod-a"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"ready": True, "restartCount": 0},
                    {"ready": False, "restartCount": 237},
                ],
            },
        },
        {
            "metadata": {"name": "pod-b"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"ready": True, "restartCount": 0},
                    {"ready": False, "restartCount": 15},
                ],
            },
        },
    ]}
    facts = deterministic_observation(
        "pods_list", {"namespace": "uat-zauh"}, result, "kkbtest"
    )
    assert facts["pod_count"] == 2
    assert facts["ready_count"] == 0
    assert facts["non_ready_count"] == 2
    assert facts["total_restarts"] == 252
    assert facts["max_restart_count"] == 237
    assert set(facts) <= {
        "resource_count", "pod_count", "ready_count", "non_ready_count",
        "total_restarts", "max_restart_count", "problematic_pod_names",
        "phase_counts",
    }
