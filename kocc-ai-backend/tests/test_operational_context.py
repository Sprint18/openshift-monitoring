from __future__ import annotations

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.conversation import (
    ActiveInspection, ConversationContext, SafeTurn, operational_history,
)
from app.main import create_app
from tests.test_agent import FakeLLM, FakeMCP, configured, tool_call
from tests.test_ai_backend import settings


def investigation_context(cluster: str = "kkbtest") -> ConversationContext:
    return ConversationContext(
        active_cluster_ids=(cluster,),
        active_inspection=ActiveInspection(
            inspection_type="pod_health", resource_kind="Pod",
            cluster_id=cluster, pod_count=4, non_ready_count=2,
            problematic_pod_names=("oneagent-a", "csi-b"),
        ),
        previous_operational_intent="inspect_pods",
    )


def history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "problemli podları kontrol et"},
        {"role": "assistant", "content": "En kritik zincir Dynatrace tarafında görünüyor."},
    ]


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
            "conversation_context": investigation_context().public_dict(),
        },
    )
    assert response.status_code == 200
    call = agent_class.return_value.run.call_args
    assert "podların" in call.args[0]
    assert [turn.content for turn in call.kwargs["semantic_history"]] == [
        item["content"] for item in history()
    ]
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
            "conversation_context": investigation_context("rmtest").public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "rmtest"
    agent_class.return_value.run.assert_called_once()
    assert "event" in agent_class.return_value.run.call_args.args[0]


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_short_operational_clarification_uses_only_its_conversation(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh dynatrace", [], [])
    client = TestClient(create_app(settings(token="token")))
    contextual = client.post("/api/v1/chat", json={
        "message": "dynatrace", "recent_turns": history(),
        "conversation_context": investigation_context().public_dict(),
    })
    assert contextual.status_code == 200
    assert agent_class.return_value.run.call_count == 1
    assert contextual.json()["conversation_context"]["investigation_focus"] == "dynatrace"

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
        "raw": {"token": "never"},
    })
    assert malformed.active_cluster_ids == ("kkbtest",)
    assert malformed.investigation_focus is None
    assert malformed.previous_operational_intent is None
    assert "raw" not in malformed.public_dict()


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
