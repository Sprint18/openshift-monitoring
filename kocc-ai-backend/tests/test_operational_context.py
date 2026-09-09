from __future__ import annotations

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.conversation import (
    ActiveInspection, ConversationContext, SafeTurn, operational_history,
)
from app.evidence import EvidenceEnvelope, EvidenceResource
from app.main import create_app
from tests.test_agent import FakeLLM, FakeMCP, configured, tool_call
from tests.test_ai_backend import settings


def investigation_context(
    cluster: str = "kkbtest", focus: str | None = None,
) -> ConversationContext:
    return ConversationContext(
        active_cluster_ids=(cluster,),
        active_inspection=ActiveInspection(
            inspection_type="pod_health", resource_kind="Pod",
            cluster_id=cluster, pod_count=4, non_ready_count=2,
            problematic_pod_names=("oneagent-a", "csi-b"),
        ),
        investigation_focus=focus,
        previous_operational_intent="inspect_pods",
    )


def history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "problemli podları kontrol et"},
        {"role": "assistant", "content": "En kritik zincir Dynatrace tarafında görünüyor."},
    ]


def pod_evidence(*, namespace: str | None = None) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        cluster_id="kkbtest", operation="inspect",
        resource=EvidenceResource(
            api_version="v1", kind="Pod", namespace=namespace,
        ),
        completeness="partial",
        facts={
            "pod_count": 2, "ready_count": 0, "non_ready_count": 2,
            "problematic_pod_names": ["oneagent-a", "csi-b"],
            "problematic_namespaces": ["dynatrace", "application-a"],
        },
        provenance={"tool": (
            "pods_list_in_namespace" if namespace else "pods_list"
        )},
    )


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_exact_operational_chain_preserves_selected_focus_and_fresh_path(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.side_effect = [
        AgentResult("Sorunlu podlar", [], [pod_evidence()]),
        AgentResult(
            "Dynatrace en kritik sorun.", [], [], 1, "dynatrace",
        ),
        AgentResult(
            "Güncel Dynatrace podları", [{
                "name": "pods_list_in_namespace", "status": "success",
            }], [pod_evidence(namespace="dynatrace")],
        ),
    ]
    client = TestClient(create_app(settings(token="token")))
    first = client.post("/api/v1/chat", json={
        "message": "kkbtest clusterinda problemli olan podları kontrol et ve bana özetle",
    })
    second = client.post("/api/v1/chat", json={
        "message": "bunlar içinde en kritik olan hangisi , neden ?",
        "recent_turns": history(),
        "conversation_context": first.json()["conversation_context"],
    })
    assert second.json()["conversation_context"]["investigation_focus"] == "dynatrace"
    third = client.post("/api/v1/chat", json={
        "message": "onun namespace'indeki diger podların durumuna da bak",
        "recent_turns": history(),
        "conversation_context": second.json()["conversation_context"],
    })
    assert third.status_code == 200
    assert third.json()["cluster"] == "kkbtest"
    assert third.json()["evidence"][0]["tool"] == "pods_list_in_namespace"
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "pods_list_in_namespace", {"namespace": "dynatrace"},
    )


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_namespace_anaphora_routes_fresh_agent_with_semantic_history(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "onun namespaceindeki diger podların durumuna da bak",
            "recent_turns": history(),
            "conversation_context": investigation_context(focus="dynatrace").public_dict(),
        },
    )
    assert response.status_code == 200
    call = agent_class.return_value.run.call_args
    assert "podların" in call.args[0]
    assert [turn.content for turn in call.kwargs["semantic_history"]] == [
        item["content"] for item in history()
    ]
    assert call.kwargs["required_fresh_tool"] == (
        "pods_list_in_namespace", {"namespace": "dynatrace"}
    )
    assert response.json()["tool_calls"] == []


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_previous_problem_events_keep_cluster_and_require_fresh_agent(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh events", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "ilk baktığımız problemle ilişkili olabilecek eventleri kontrol et",
            "recent_turns": history(),
            "conversation_context": investigation_context(
                "rmtest", "dynatrace"
            ).public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "rmtest"
    agent_class.return_value.run.assert_called_once()
    assert "event" in agent_class.return_value.run.call_args.args[0]
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "events_list", {"namespace": "dynatrace"}
    )


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_short_operational_clarification_uses_only_its_conversation(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh dynatrace", [], [])
    client = TestClient(create_app(settings(token="token")))
    clarification = client.post("/api/v1/chat", json={
        "message": "onun namespaceindeki diger podların durumuna da bak",
        "recent_turns": history(),
        "conversation_context": investigation_context().public_dict(),
    })
    assert clarification.json()["answer"] == "Hangi namespace'i kastediyorsun?"
    assert clarification.json()["conversation_context"]["pending_operational_intent"] == "inspect_pods"

    contextual = client.post("/api/v1/chat", json={
        "message": "dynatrace", "recent_turns": history(),
        "conversation_context": clarification.json()["conversation_context"],
    })
    assert contextual.status_code == 200
    assert agent_class.return_value.run.call_count == 1
    assert contextual.json()["conversation_context"]["investigation_focus"] == "dynatrace"
    assert "pending_operational_intent" not in contextual.json()["conversation_context"]

    fresh = client.post("/api/v1/chat", json={"message": "dynatrace"})
    assert fresh.status_code == 200
    assert fresh.json()["clusters"] == []
    assert agent_class.return_value.run.call_count == 1


def test_ambiguous_anaphora_without_context_asks_for_resource() -> None:
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={"message": "onun durumuna bak"},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "Hangi kaynağı kastettiğini netleştirir misin?"
    assert response.json()["evidence"] == []


def test_new_context_fields_are_validated_bounded_and_backward_compatible() -> None:
    old = ConversationContext.from_payload({"active_cluster_ids": ["kkbtest"]})
    assert old.investigation_focus is None
    malformed = ConversationContext.from_payload({
        "active_cluster_ids": ["kkbtest", "evil"],
        "investigation_focus": "not safe/namespace",
        "previous_operational_intent": "dump_raw_tool_payload",
        "pending_operational_intent": "inspect_pods",
        "pending_operational_cluster_id": "evil",
        "raw": {"token": "never"},
    })
    assert malformed.active_cluster_ids == ("kkbtest",)
    assert malformed.investigation_focus is None
    assert malformed.previous_operational_intent is None
    assert malformed.pending_operational_intent is None
    assert malformed.pending_operational_cluster_id is None
    assert "raw" not in malformed.public_dict()
    oversized = ConversationContext.from_payload({
        "investigation_focus": "a" * 1000,
        "pending_operational_intent": "inspect_pods",
        "pending_operational_cluster_id": "kkbtest",
    })
    assert oversized.investigation_focus is None


def test_operational_history_is_bounded_and_does_not_accept_raw_payload_turns() -> None:
    from app.conversation import bounded_history

    turns = [
        {"role": "assistant", "content": "ignored", "raw": {"items": [1]}},
        *({"role": "user", "content": f"turn-{index}-" + "x" * 1000}
          for index in range(10)),
    ]
    safe = operational_history(bounded_history(turns))
    assert len(safe) <= 6
    assert sum(len(turn.content) for turn in safe) <= 4000
    assert all("ignored" not in turn.content for turn in safe)


def test_historical_assistant_claim_requires_a_fresh_tool_result() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("pods_list")]},
        {"content": "Yeni sonuçtan yanıt", "tool_calls": None},
    ])
    mcp = FakeMCP([{"items": []}])
    mcp.list_tools = lambda: [{
        "name": "pods_list", "description": "List current pods",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    semantic = operational_history([SafeTurn("assistant", "Pod hâlâ failing.")])
    result = AgentLoop(
        configured(), llm, mcp, "kkbtest", "KKB TEST",
    ).run("hala failing mi?", semantic_history=semantic)
    assert result.answer == "Yeni sonuçtan yanıt"
    assert mcp.calls == [("pods_list", {})]
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "never live\ncluster evidence" in prompt


@patch("app.main.MCPClient")
def test_pending_namespace_executes_required_fresh_pod_tool(
    mcp_class: Mock,
) -> None:
    mcp = FakeMCP([{"items": []}])
    mcp.list_tools = lambda: [{
        "name": "pods_list_in_namespace", "description": "List namespace pods",
        "inputSchema": {
            "type": "object", "properties": {"namespace": {"type": "string"}},
            "required": ["namespace"],
        },
    }]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    llm = FakeLLM([{
        "content": "Güncel pod sonucu.", "tool_calls": None,
    }])
    llm.is_configured = lambda: True
    application.state.llm_client = llm
    pending = investigation_context().with_pending_operational(
        "inspect_pods", "kkbtest"
    )
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "dynatrace", "conversation_context": pending.public_dict(),
    })
    assert response.status_code == 200
    assert mcp.calls == [("pods_list_in_namespace", {"namespace": "dynatrace"})]
    assert response.json()["evidence"][0]["tool"] == "pods_list_in_namespace"


def test_assistant_focus_is_accepted_only_from_bounded_candidates() -> None:
    llm = FakeLLM([{
        "content": "Dynatrace en kritik adaydır.", "tool_calls": None,
    }])
    mcp = FakeMCP()
    result = AgentLoop(configured(), llm, mcp).run(
        "bunlar içinde en kritik olan hangisi?",
        focus_candidates=("dynatrace", "app-team"),
    )
    assert result.focus_namespace == "dynatrace"

    ambiguous = AgentLoop(configured(), FakeLLM([{
        "content": "Dynatrace ve app-team incelenmeli.", "tool_calls": None,
    }]), FakeMCP()).run(
        "hangisi?", focus_candidates=("dynatrace", "app-team"),
    )
    assert ambiguous.focus_namespace is None


def test_required_fresh_tool_failure_does_not_reuse_history() -> None:
    llm = FakeLLM([])
    mcp = FakeMCP([{"isError": True}])
    mcp.list_tools = lambda: [{
        "name": "pods_list_in_namespace", "description": "List namespace pods",
        "inputSchema": {
            "type": "object", "properties": {"namespace": {"type": "string"}},
            "required": ["namespace"],
        },
    }]
    result = AgentLoop(configured(), llm, mcp).run(
        "hala failing mi?",
        semantic_history=[SafeTurn("assistant", "Evet, failing.")],
        required_fresh_tool=(
            "pods_list_in_namespace", {"namespace": "dynatrace"},
        ),
    )
    assert result.tool_calls == [{
        "name": "pods_list_in_namespace", "status": "error",
    }]
    assert "araç çağrısı" in result.answer
    assert "Evet, failing" not in result.answer
    assert llm.calls == []


def test_scheduler_message_cannot_become_cluster_wide_cpu_exhaustion() -> None:
    answer = (
        "0/16 nodes are available: 1 Insufficient cpu. "
        "Bu, tüm cluster CPU kapasitesinin tükendiğini gösteriyor."
    )
    result = AgentLoop(
        configured(), FakeLLM([{"content": answer, "tool_calls": None}]), FakeMCP(),
    ).run("neden schedule olmadı?")
    assert "tükendiğini gösteriyor" not in result.answer
    assert "ek node/request/capacity kanıtı gerekir" in result.answer


def test_cluster_switch_drops_foreign_investigation_focus() -> None:
    switched = investigation_context("kkbtest", "dynatrace").with_active_clusters((
        "rmtest",
    ))
    assert switched.active_inspection is None
    assert switched.investigation_focus is None
    assert switched.previous_operational_intent is None


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_cluster_override_does_not_receive_foreign_focus(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("rm fresh", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "RMTEST'teki node durumuna bak",
            "recent_turns": history(),
            "conversation_context": investigation_context(
                "kkbtest", "dynatrace"
            ).public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "rmtest"
    call = agent_class.return_value.run.call_args
    assert call.args == ("node durumuna bak",)
    assert call.kwargs == {}


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_still_failing_followup_requires_fresh_pods(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh", [], [])
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "hala sorun var mı?",
            "conversation_context": investigation_context(
                "kkbtest", "dynatrace"
            ).public_dict(),
        },
    )
    assert response.status_code == 200
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "pods_list_in_namespace", {"namespace": "dynatrace"},
    )
