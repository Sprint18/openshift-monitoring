from __future__ import annotations

import json
import io
import logging
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.conversation import ActiveInspection, ConversationContext, SafeTurn, TriageCandidate, TriageResource
from app.main import create_app
from app.triage import decide_triage
from tests.test_ai_backend import settings
from tests.test_phase4d import _resource_tool


def _egress(name: str, *, assigned: bool = True) -> dict:
    return {
        "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
        "metadata": {"name": name},
        "spec": {
            "egressIPs": ["10.60.1.222"],
            "namespaceSelector": {"matchLabels": {"team": "ai"}},
            "podSelector": {},
        },
        **({"status": {"items": [{
            "egressIP": "10.60.1.222", "node": "worker-a",
        }]}} if assigned else {}),
    }


def test_real_deployed_egressip_envelope_is_parsed_without_detail_calls() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {
        "structuredContent": {"result": {"content": [{
            "type": "text",
            "text": json.dumps({"kind": "EgressIPList", "items": [
                _egress("egress-ai"), _egress("egress-waiting", assigned=False),
            ]}),
        }]}}
    }
    llm = Mock()
    result = AgentLoop(
        settings(token=None), llm, mcp, "kkbtest", "KKB TEST",
    ).run("Bünyendeki tüm egress ip'leri bana sıralar mısın?")
    assert "egress-ai" in result.answer
    assert "worker-a" in result.answer
    assert "egress-waiting" in result.answer
    assert "Yapılandırılmış EgressIP: `10.60.1.222`" in result.answer
    assert "Henüz bir node ataması gözlemlenmedi" in result.answer
    assert mcp.call_tool.call_count == 1
    llm.chat_completion.assert_not_called()


def test_malformed_nested_egressip_json_is_parse_error() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {
        "structuredContent": {"result": {"content": [{
            "type": "text", "text": '{"items": [',
        }]}}
    }
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("kocc_ai.agent").addHandler(handler)
    try:
        result = AgentLoop(
            settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST",
        ).run("tüm egress ipleri listele")
    finally:
        logging.getLogger("kocc_ai.agent").removeHandler(handler)
    assert "doğrulanamadı" in result.answer
    assert "egressip_result status=parse_error" in stream.getvalue()


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_cluster_scope_clears_stale_namespace_and_pending_operation(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh", [], [])
    stale = ConversationContext(
        active_cluster_ids=("kkbtest",),
        active_inspection=ActiveInspection(
            "pod_health", "Pod", "kkbtest", namespace="test-yapayzekarag",
        ),
        investigation_focus="test-yapayzekarag",
        previous_operational_intent="inspect_pods",
        pending_operational_intent="inspect_events",
        pending_operational_cluster_id="kkbtest",
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "kkbtest clusterinda problemli olan podlar hangisi kontrol et",
            "conversation_context": stale.public_dict(),
        },
    )
    assert response.status_code == 200
    call = agent_class.return_value.run.call_args
    assert "test-yapayzekarag" not in call.args[0]
    assert call.kwargs == {}
    context = response.json()["conversation_context"]
    assert context.get("investigation_focus") is None
    assert context.get("pending_operational_intent") is None
    assert context["active_inspection"] is None


def test_triage_prompt_is_canonical_and_excludes_stale_prose() -> None:
    llm = Mock()
    llm.chat_completion.return_value = {"content": json.dumps({
        "assessment": "highest", "selected_namespace": "alpha",
        "tied_namespaces": [], "selected_resources": ["pod-a"],
        "confidence": "high", "reasons": ["Grounded failure."],
    })}
    candidates = (
        TriageCandidate("zeta", (TriageResource("Pod", "pod-z", "Pending", False, 0, ()),)),
        TriageCandidate("alpha", (TriageResource("Pod", "pod-a", "Pending", False, 0, ()),)),
    )
    for _ in range(2):
        decide_triage(
            llm, question="hangisi en kritik?", cluster_id="kkbtest",
            candidates=candidates,
            semantic_history=[SafeTurn("assistant", "stale namespace: old")],
        )
    first = llm.chat_completion.call_args_list[0].args[0]
    second = llm.chat_completion.call_args_list[1].args[0]
    assert first == second
    assert "stale namespace" not in json.dumps(first)
    request = json.loads(first[1]["content"])
    assert [item["namespace"] for item in request["comparison_candidates"]] == [
        "alpha", "zeta",
    ]


def test_triage_can_compare_multiple_resources_in_one_namespace() -> None:
    llm = Mock()
    llm.chat_completion.return_value = {"content": json.dumps({
        "assessment": "highest", "selected_namespace": "test-ai",
        "tied_namespaces": [], "selected_resources": ["backend"],
        "confidence": "medium", "reasons": ["CrashLoopBackOff observed."],
    })}
    decision = decide_triage(
        llm, question="bunlar içinde en kritik olan hangisi?",
        cluster_id="kkbtest",
        candidates=(TriageCandidate("test-ai", (
            TriageResource("Pod", "backend", "Running", False, 4, ("CrashLoopBackOff",)),
            TriageResource("Pod", "worker", "Pending", False, 0, ("ContainerCreating",)),
        )),),
    )
    assert decision.selected_namespace == "test-ai"
    assert decision.selected_resources == ("backend",)
    assert "`backend`" in decision.render_answer()
