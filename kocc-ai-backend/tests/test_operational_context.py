from __future__ import annotations

import importlib.util
import json
from pathlib import Path
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
            problematic_namespaces=("dynatrace", "application-a"),
        ),
        investigation_focus=focus,
        previous_operational_intent="inspect_pods",
    )


def history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "problemli podları kontrol et"},
        {"role": "assistant", "content": "En kritik zincir Dynatrace tarafında görünüyor."},
    ]


def pod_evidence(
    *, namespace: str | None = None,
    problematic_namespaces: list[str] | None = None,
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        cluster_id="kkbtest", operation="inspect",
        resource=EvidenceResource(
            api_version="v1", kind="Pod", namespace=namespace,
        ),
        completeness="partial",
        facts={
            "pod_count": 2, "ready_count": 0, "non_ready_count": 2,
            "problematic_pod_names": ["oneagent-a", "csi-b"],
            "problematic_namespaces": problematic_namespaces or [
                "dynatrace", "application-a",
            ],
        },
        provenance={"tool": (
            "pods_list_in_namespace" if namespace else "pods_list"
        )},
    )


def production_proxy_context(value: dict) -> dict:
    path = Path(__file__).parents[2] / "kocc-v0.4.0/app/ai_client.py"
    spec = importlib.util.spec_from_file_location("portal_ai_client_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AIBackendClient._conversation_context(value)


def runtime_pod_result(items: list[dict]) -> dict:
    payload = json.dumps({
        "apiVersion": "v1", "kind": "PodList", "items": items,
    })
    return {"structuredContent": {"result": {"content": [{
        "type": "text", "text": payload,
    }]}}}


def pod(name: str, namespace: str, phase: str, ready: bool) -> dict:
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "status": {"phase": phase, "containerStatuses": [{
            "ready": ready, "restartCount": 0,
            "state": ({"running": {}} if ready else {
                "waiting": {"reason": "ImagePullBackOff"},
            }),
        }]},
    }


@patch("app.main.MCPClient")
def test_real_turn_one_evidence_survives_proxy_and_drives_followup(
    mcp_class: Mock,
) -> None:
    cluster_pods = [
        pod("sonarqube", "lab-sdlc", "Pending", False),
        pod("init-failure", "lab-sdlc", "Pending", False),
        pod("oneagent", "dynatrace", "Pending", False),
        pod("image-pull", "mw-test2", "Pending", False),
        pod("healthy", "unrelated-healthy", "Running", True),
        pod("completed", "completed-job", "Succeeded", False),
    ]
    mcp = FakeMCP([
        runtime_pod_result(cluster_pods),
        {"content": [{"type": "text", "text": "event evidence"}]},
        runtime_pod_result([
            pod("sonarqube", "lab-sdlc", "Pending", False),
            pod("scanner", "lab-sdlc", "Running", True),
        ]),
    ])
    mcp.list_tools = lambda: [{
        "name": name, "description": name,
        "inputSchema": {
            "type": "object",
            "properties": ({"namespace": {"type": "string"}}
                           if name != "pods_list" else {}),
            **({"required": ["namespace"]}
               if name == "pods_list_in_namespace" else {}),
        },
    } for name in ("pods_list", "events_list", "pods_list_in_namespace")]
    mcp_class.return_value = mcp
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("pods_list")]},
        {"content": None, "tool_calls": [tool_call("events_list")]},
        {"content": "Problemli pod özeti.", "tool_calls": None},
        {"content": (
            "En kritik lab-sdlc; dynatrace ve mw-test2 de karşılaştırıldı.\n"
            "[[KOCC_SELECTED_FOCUS:lab-sdlc]]"
        ), "tool_calls": None},
        {"content": "lab-sdlc güncel pod durumu.", "tool_calls": None},
    ])
    llm.is_configured = lambda: True
    application = create_app(settings(token="token"))
    application.state.llm_client = llm
    client = TestClient(application)

    first = client.post("/api/v1/chat", json={
        "message": "kkbtest clusterinda problemli olan podları kontrol et ve bana özetle",
    })
    assert first.status_code == 200
    first_context = first.json()["conversation_context"]
    assert first_context["active_inspection"]["problematic_namespaces"] == [
        "lab-sdlc", "dynatrace", "mw-test2",
    ]
    assert "unrelated-healthy" not in first_context["active_inspection"][
        "problematic_namespaces"
    ]
    assert "completed-job" not in first_context["active_inspection"][
        "problematic_namespaces"
    ]

    proxied_context = production_proxy_context(first_context)
    assert proxied_context["active_cluster_ids"] == ["kkbtest"]
    assert proxied_context["active_inspection"]["problematic_namespaces"] == [
        "lab-sdlc", "dynatrace", "mw-test2",
    ]
    assert proxied_context["previous_operational_intent"] == "inspect_pods"
    calls_after_first = list(mcp.calls)
    second = client.post("/api/v1/chat", json={
        "message": "bunlar içinde en kritik olan hagisi , neden ?",
        "conversation_context": proxied_context,
    })
    assert second.status_code == 200
    assert mcp.calls == calls_after_first
    assert second.json()["conversation_context"]["investigation_focus"] == "lab-sdlc"
    focus_prompt = llm.calls[3]["messages"][0]["content"]
    assert "SEMANTIC FOCUS CONTRACT" in focus_prompt

    third = client.post("/api/v1/chat", json={
        "message": "onun namespace'indeki diger podların durumuna da bak",
        "conversation_context": production_proxy_context(
            second.json()["conversation_context"]
        ),
    })
    assert third.status_code == 200
    assert third.json()["answer"] != "Hangi namespace'i kastediyorsun?"
    assert mcp.calls[-1] == (
        "pods_list_in_namespace", {"namespace": "lab-sdlc"},
    )
    assert third.json()["evidence"][0]["tool"] == "pods_list_in_namespace"


@patch("app.main.MCPClient")
def test_semantic_focus_drives_fresh_followups_without_active_inspection(
    mcp_class: Mock,
) -> None:
    mcp = FakeMCP([
        {"content": [{"type": "text", "text": "grounded pod output"}]},
        {"content": [{"type": "text", "text": "grounded event output"}]},
        runtime_pod_result([
            pod("python-app", "uat-greendeks", "Pending", False),
        ]),
        {"content": [{"type": "text", "text": "fresh namespace events"}]},
    ])
    mcp.list_tools = lambda: [{
        "name": name, "description": name,
        "inputSchema": {
            "type": "object",
            "properties": ({"namespace": {"type": "string"}}
                           if name != "pods_list" else {}),
            **({"required": ["namespace"]}
               if name == "pods_list_in_namespace" else {}),
        },
    } for name in ("pods_list", "events_list", "pods_list_in_namespace")]
    mcp_class.return_value = mcp
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("pods_list")]},
        {"content": None, "tool_calls": [tool_call("events_list")]},
        {"content": (
            "Problemli namespace'ler: lab-sdlc, dynatrace, uat-greendeks, "
            "openshift-storage."
        ), "tool_calls": None},
        {"content": (
            "En kritik: uat-greendeks/python-app-56f8b78f84-dxm5f; "
            "diğer namespace'lerle karşılaştırıldığında önce incelenmeli.\n"
            "[[KOCC_SELECTED_FOCUS:uat-greendeks]]"
        ), "tool_calls": None},
        {"content": "uat-greendeks için fresh pod sonucu.", "tool_calls": None},
        {"content": "uat-greendeks için fresh event sonucu.", "tool_calls": None},
    ])
    llm.is_configured = lambda: True
    application = create_app(settings(token="token"))
    application.state.llm_client = llm
    client = TestClient(application)

    first = client.post("/api/v1/chat", json={
        "message": "kkbtest clusterinda problemli olan podları kontrol et ve bana özetle",
    })
    assert first.status_code == 200
    assert first.json()["conversation_context"]["active_inspection"] is None
    assert first.json()["conversation_context"][
        "previous_operational_intent"
    ] == "inspect_pods"
    calls_after_first = list(mcp.calls)

    second = client.post("/api/v1/chat", json={
        "message": "bunlar içinde en kritik olan hangisi , neden ?",
        "recent_turns": [{
            "role": "assistant", "content": first.json()["answer"],
        }],
        "conversation_context": production_proxy_context(
            first.json()["conversation_context"]
        ),
    })
    assert second.status_code == 200
    assert mcp.calls == calls_after_first
    assert "KOCC_SELECTED_FOCUS" not in second.json()["answer"]
    assert second.json()["conversation_context"][
        "investigation_focus"
    ] == "uat-greendeks"

    third = client.post("/api/v1/chat", json={
        "message": "onun namespace'indeki diğer podları da kontrol eder misin",
        "conversation_context": production_proxy_context(
            second.json()["conversation_context"]
        ),
    })
    assert third.status_code == 200
    assert third.json()["answer"] != "Hangi namespace'i kastediyorsun?"
    assert mcp.calls[-1] == (
        "pods_list_in_namespace", {"namespace": "uat-greendeks"},
    )
    assert third.json()["evidence"][0]["tool"] == "pods_list_in_namespace"

    fourth = client.post("/api/v1/chat", json={
        "message": "onunla ilgili eventlere de bak",
        "conversation_context": production_proxy_context(
            third.json()["conversation_context"]
        ),
    })
    assert fourth.status_code == 200
    assert mcp.calls[-1] == (
        "events_list", {"namespace": "uat-greendeks"},
    )
    assert fourth.json()["evidence"][0]["tool"] == "events_list"


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_exact_operational_chain_preserves_selected_focus_and_fresh_path(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.side_effect = [
        AgentResult("Sorunlu podlar", [], [pod_evidence(
            problematic_namespaces=["lab-sdlc", "dynatrace", "mw-test2"],
        )]),
        AgentResult(
            "En kritik Dynatrace. lab-sdlc ve mw-test2 daha düşük öncelikli.",
            [], [], 1, "dynatrace",
        ),
        AgentResult(
            "Güncel Dynatrace podları", [{
                "name": "pods_list_in_namespace", "status": "success",
            }], [pod_evidence(namespace="dynatrace")],
        ),
        AgentResult(
            "Güncel Dynatrace eventleri", [{
                "name": "events_list", "status": "success",
            }], [{"tool": "events_list", "status": "success"}],
        ),
    ]
    client = TestClient(create_app(settings(token="token")))
    first = client.post("/api/v1/chat", json={
        "message": "kkbtest clusterinda problemli olan podları kontrol et ve bana özetle",
    })
    second = client.post("/api/v1/chat", json={
        "message": "bunlar içinde en kritik olan hagisi , neden ?",
        "recent_turns": history(),
        "conversation_context": first.json()["conversation_context"],
    })
    assert second.json()["conversation_context"]["investigation_focus"] == "dynatrace"
    second_call = agent_class.return_value.run.call_args_list[1]
    assert second_call.kwargs["required_fresh_tool"] is None
    assert second_call.kwargs["focus_selection_requested"] is True
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
    fourth = client.post("/api/v1/chat", json={
        "message": "ilk baktığımız problemle ilişkili olabilecek eventleri kontrol et",
        "recent_turns": history(),
        "conversation_context": third.json()["conversation_context"],
    })
    assert fourth.status_code == 200
    assert fourth.json()["evidence"][0]["tool"] == "events_list"
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "events_list", {"namespace": "dynatrace"},
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


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_pending_reference_to_structured_focus_resolves_without_guessing(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh", [], [])
    pending = investigation_context(focus="dynatrace").with_pending_operational(
        "inspect_pods", "kkbtest",
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "az önce en kritik dediğin",
            "conversation_context": pending.public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["conversation_context"]["investigation_focus"] == "dynatrace"
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "pods_list_in_namespace", {"namespace": "dynatrace"},
    )


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
def test_failed_cluster_pod_listing_does_not_create_candidates(
    mcp_class: Mock,
) -> None:
    mcp = FakeMCP([{"isError": True}])
    mcp.list_tools = lambda: [{
        "name": "pods_list", "description": "List current pods",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    mcp_class.return_value = mcp
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("pods_list")]},
        {"content": "Güncel pod verisi alınamadı.", "tool_calls": None},
    ])
    llm.is_configured = lambda: True
    application = create_app(settings(token="token"))
    application.state.llm_client = llm
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "kkbtest clusterinda problemli olan podları kontrol et ve bana özetle",
    })
    assert response.status_code == 200
    assert response.json()["conversation_context"]["active_inspection"] is None


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


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_pending_explicit_namespace_uses_fresh_evidence_without_candidate_gate(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh", [], [])
    pending = investigation_context().with_pending_operational(
        "inspect_pods", "kkbtest"
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "made-up-prod",
            "conversation_context": pending.public_dict(),
        },
    )
    assert response.status_code == 200
    assert agent_class.return_value.run.call_args.kwargs["required_fresh_tool"] == (
        "pods_list_in_namespace", {"namespace": "made-up-prod"},
    )


def test_assistant_semantic_focus_is_validated_without_evidence_candidates() -> None:
    llm = FakeLLM([{
        "content": (
            "Dynatrace en kritik adaydır; app-team ile karşılaştırıldı.\n"
            "[[KOCC_SELECTED_FOCUS:dynatrace]]"
        ), "tool_calls": None,
    }])
    mcp = FakeMCP()
    result = AgentLoop(configured(), llm, mcp, "kkbtest", "KKB TEST").run(
        "bunlar içinde en kritik olan hangisi?",
        focus_selection_requested=True,
    )
    assert result.focus_namespace == "dynatrace"
    assert "KOCC_SELECTED_FOCUS" not in result.answer
    assert "app-team" in result.answer

    invalid_answers = (
        "Dynatrace en kritik, app-team de incelendi.",
        "Seçim. [[KOCC_SELECTED_FOCUS:made_up_prod]]",
        "Seçim. [[KOCC_SELECTED_FOCUS:rmtest]]",
        "Seçim. [[KOCC_SELECTED_FOCUS:" + "a" * 64 + "]]",
        "Seçim. [[KOCC_SELECTED_FOCUS:dynatrace]] "
        "[[KOCC_SELECTED_FOCUS:app-team]]",
        "Seçim. [[KOCC_SELECTED_FOCUS:dynatrace",
    )
    for answer in invalid_answers:
        invalid = AgentLoop(configured(), FakeLLM([{
            "content": answer, "tool_calls": None,
        }]), FakeMCP(), "kkbtest", "KKB TEST").run(
            "hangisi?",
            focus_selection_requested=True,
        )
        assert invalid.focus_namespace is None
        assert "KOCC_SELECTED_FOCUS" not in invalid.answer

    no_cluster = AgentLoop(configured(), FakeLLM([{
        "content": "Seçim. [[KOCC_SELECTED_FOCUS:dynatrace]]",
        "tool_calls": None,
    }]), FakeMCP()).run("hangisi?", focus_selection_requested=True)
    assert no_cluster.focus_namespace is None
    assert "KOCC_SELECTED_FOCUS" not in no_cluster.answer


@patch("app.main.MCPClient")
def test_api_analysis_followup_sets_focus_without_unnecessary_tool_call(
    mcp_class: Mock,
) -> None:
    mcp = FakeMCP()
    mcp_class.return_value = mcp
    llm = FakeLLM([{
        "content": (
            "En kritik Dynatrace; lab-sdlc ve mw-test2 ile karşılaştırıldı.\n"
            "[[KOCC_SELECTED_FOCUS:dynatrace]]"
        ),
        "tool_calls": None,
    }])
    llm.is_configured = lambda: True
    application = create_app(settings(token="token"))
    application.state.llm_client = llm
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "bunlar içinde en kritik olan hagisi , neden ?",
        "conversation_context": ConversationContext(
            active_cluster_ids=("kkbtest",), active_inspection=None,
            previous_operational_intent="inspect_pods",
        ).public_dict(),
    })
    assert response.status_code == 200
    assert response.json()["conversation_context"]["investigation_focus"] == "dynatrace"
    assert mcp.calls == []
    assert mcp.list_count == 0
    assert "KOCC_SELECTED_FOCUS" not in response.json()["answer"]


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
        "Bu, tüm cluster CPU kapasitesinin tükendiğini gösteriyor. "
        "Pod CPU isteğini düşürün veya node kapasitesi ekleyin."
    )
    result = AgentLoop(
        configured(), FakeLLM([{"content": answer, "tool_calls": None}]), FakeMCP(),
    ).run("neden schedule olmadı?")
    assert "tükendiğini gösteriyor" not in result.answer
    assert "isteğini düşürün" not in result.answer
    assert "kapasitesi ekleyin" not in result.answer
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


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_analysis_followup_stays_operational_without_inspection_payload(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("analysis", [], [])
    context = ConversationContext(
        active_cluster_ids=("kkbtest",),
        previous_operational_intent="inspect_pods",
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "bunlar içinde en kritik olan hagisi, neden?",
            "conversation_context": context.public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "kkbtest"
    agent_class.return_value.run.assert_called_once()
