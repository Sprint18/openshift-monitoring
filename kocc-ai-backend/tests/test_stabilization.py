from __future__ import annotations

import json
import io
import logging
import subprocess
import sys
from unittest.mock import Mock, call, patch

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.conversation import ActiveInspection, ConversationContext, SafeTurn, TriageCandidate, TriageResource
from app.egressip import (
    egressip_namespace, egressip_query_mode,
)
from app.main import create_app
from app.mcp_client import MCPClient, parse_mcp_body
from app.mcp_normalization import (
    mcp_result_shape, normalize_mcp_result, resource_items,
)
from app.triage import decide_triage
from tests.test_ai_backend import settings
from tests.test_phase4d import _egress_mcp, _resource_get_tool, _resource_tool


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


class SerializedMCPClient(MCPClient):
    """Exercise JSON-RPC body parsing and MCPClient._rpc without live network."""

    def __init__(self, responses: list[dict]) -> None:
        super().__init__("http://fixture.invalid/mcp", 1)
        self.responses = [json.dumps(item).encode() for item in responses]

    def _post(self, payload: dict, expect_response: bool = True) -> dict:
        response = parse_mcp_body("application/json", self.responses.pop(0))
        if payload.get("method") == "initialize":
            self.session_id = "bounded-test-session"
        return response


def rpc_result(result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def test_imports_succeed_when_yaml_package_is_unavailable() -> None:
    script = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'yaml' or name.startswith('yaml.'):
        raise ModuleNotFoundError("No module named 'yaml'")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import app.mcp_normalization
import app.agent
import app.main
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_serialized_transport_content_text_json_reaches_egressip_renderer() -> None:
    object_text = json.dumps({
        "apiVersion": "k8s.ovn.org/v1",
        "kind": "EgressIP",
        "metadata": {"name": "egress-ai"},
        "spec": {
            "egressIPs": ["10.60.1.222"],
            "namespaceSelector": {"matchLabels": {"team": "ai"}},
            "podSelector": {},
        },
        "status": {
            "items": [{"egressIP": "10.60.1.222", "node": "worker-ai"}],
        },
    })
    mcp = SerializedMCPClient([
        rpc_result({"protocolVersion": "2025-03-26"}), {},
        rpc_result({"tools": [_resource_tool(), _resource_get_tool()]}),
        rpc_result({"content": [{"type": "text", "text": (
            "APIVERSION KIND NAME AGE\n"
            "k8s.ovn.org/v1 EgressIP egress-ai 1d\n"
        )}]}),
        rpc_result({"content": [{"type": "text", "text": object_text}]}),
    ])
    llm = Mock()
    result = AgentLoop(
        settings(token=None), llm, mcp, "kkbtest", "KKB TEST",
    ).run("bünyendeki tüm egress ip'leri listeler misin")
    assert "egress-ai" in result.answer
    assert "10.60.1.222" in result.answer
    assert "worker-ai" in result.answer
    assert not mcp.responses
    llm.chat_completion.assert_not_called()


def test_content_text_table_and_safe_shape_are_normalized_without_value_logging() -> None:
    result = {"content": [{"type": "text", "text": (
        "APIVERSION KIND NAME AGE\n"
        "k8s.ovn.org/v1 EgressIP confidential-egress 1d\n"
    )}]}
    normalized = normalize_mcp_result(result)
    assert normalized.status == "success"
    assert normalized.source == "content_text_table"
    assert normalized.table_rows[0]["Name"] == "confidential-egress"
    shape = mcp_result_shape(result)
    assert "text_length=" in shape and "table=true" in shape
    assert "confidential-egress" not in shape


def test_unsupported_text_is_safe_and_distinct_from_malformed() -> None:
    result = {"content": [{
        "type": "text", "text": "apiVersion: v1\nkind: Secret\nmetadata: hidden",
    }]}
    normalized = normalize_mcp_result(result)
    assert normalized.status == "unsupported"
    shape = mcp_result_shape(result)
    assert "status=unsupported" in shape
    assert "plain=true" in shape
    assert "metadata: hidden" not in shape


@patch("app.main.MCPClient")
def test_explicit_kkbtest_inventory_phrase_uses_only_cluster_list(
    mcp_class: Mock,
) -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {"content": [{
        "type": "text", "text": json.dumps([{
            "apiVersion": "k8s.ovn.org/v1",
            "kind": "EgressIP",
            "metadata": {"name": "egress-ai"},
            "spec": {
                "egressIPs": ["10.60.1.222"],
                "namespaceSelector": {},
                "podSelector": {},
            },
        }]),
    }]}
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "kkbtest ortamındaki egressip'leri listele",
    })
    assert response.status_code == 200
    assert response.json()["cluster"] == "kkbtest"
    assert "egress-ai" in response.json()["answer"]
    assert mcp.call_tool.call_args_list == [call(
        "resources_list", {"apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP"},
    )]
    application.state.llm_client.chat_completion.assert_not_called()


def test_real_deployed_egressip_envelope_is_parsed_without_detail_calls() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {
        "content": [{"type": "text", "text": "KIND EGRESSIP NAME"}],
        "structuredContent": {"items": [
            _egress("egress-ai"), _egress("egress-waiting", assigned=False),
        ]},
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


def test_official_table_envelope_is_bounded_and_does_not_require_detail_calls() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {
        "content": [{"type": "text", "text": "NAME AGE\neg-a 1d"}],
        "structuredContent": {"items": [{"Name": "eg-a", "Age": "1d"}]},
    }
    result = AgentLoop(
        settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST",
    ).run("bünyendeki tüm egress ipleri listele")
    assert "eg-a" in result.answer
    assert "liste yanıtında sunulmadı" in result.answer
    assert mcp.call_tool.call_count == 1
    assert "eg-a" not in mcp_result_shape(mcp.call_tool.return_value)


def test_egressip_intent_exact_production_phrases() -> None:
    inventory = "bünyendeki tüm egress ip'leri bana sıralar mısın"
    assert egressip_query_mode(inventory) == "inventory"
    assert egressip_namespace(inventory) is None
    for message in (
        "test-yapayzekarag namespace'indeki egress ip nedir",
        "test-yapayzekarag namespace'indeki egress ip'si nedir",
    ):
        assert egressip_query_mode(message) == "namespace"
        assert egressip_namespace(message) == "test-yapayzekarag"


@pytest.mark.parametrize("message", (
    "test-yapayzekarag egressip'si nedir",
    "test-yapayzekarag egress ip nedir",
    "test-yapayzekarag namespace'indeki egress ip nedir",
    "test-yapayzekarag namespace'ine ait egressip nedir",
    "test-yapayzekarag namespace'inin egress ip'si nedir",
    "kkbtest ortamındaki test-yapayzekarag namespace'ine ait egressip nedir",
    "kkbtest clusterındaki test-yapayzekarag namespace egress ip nedir",
    "test-yapayzekarag hangi egress ip'yi kullanıyor",
))
@patch("app.main.MCPClient")
def test_turkish_namespace_variants_reach_exact_operational_tool_arguments(
    mcp_class: Mock, message: str,
) -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"structuredContent": {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-yapayzekarag", "labels": {"team": "ai"}},
        }]}},
        {"structuredContent": {"items": [_egress("egress-ai")]}},
    ]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    response = TestClient(application).post("/api/v1/chat", json={
        "message": message, "conversation_scope": "kkbtest",
    })
    assert response.status_code == 200
    assert response.json()["cluster"] == "kkbtest"
    assert "test-yapayzekarag" in response.json()["answer"]
    assert mcp.call_tool.call_args_list[0].args == (
        "resources_list", {"apiVersion": "v1", "kind": "Namespace"},
    )
    assert mcp.call_tool.call_args_list[1].args == (
        "resources_list", {"apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP"},
    )
    application.state.llm_client.chat_completion.assert_not_called()


@pytest.mark.parametrize("candidate", (
    "in", "nin", "nın", "un", "ün", "ait", "e", "a", "ip", "egressip",
    "namespace", "ortamındaki", "nedir", "hangisi", "tüm", "hepsi",
))
def test_turkish_grammar_tokens_never_become_egressip_namespaces(candidate: str) -> None:
    assert egressip_namespace(f"{candidate} egressip nedir") is None
    assert egressip_namespace(f"{candidate} namespace egressip nedir") is None


def test_mcp_normalization_does_not_crawl_unrelated_nested_items() -> None:
    assert resource_items({
        "structuredContent": {"audit": {"items": [_egress("not-a-result")]}}
    }) is None


@patch("app.main.MCPClient")
def test_pending_egressip_namespace_reply_resumes_without_llm(
    mcp_class: Mock,
) -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"structuredContent": {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-yapayzekarag", "labels": {"team": "ai"}},
        }]}},
        {"structuredContent": {"items": [_egress("egress-ai")]}},
    ]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    first = TestClient(application).post("/api/v1/chat", json={
        "message": "egressip", "conversation_scope": "kkbtest",
    })
    assert first.status_code == 200
    context = first.json()["conversation_context"]
    assert context["pending_operational_intent"] == "egressip_lookup"
    assert context["pending_operational_parameter"] == "namespace"
    from tests.test_operational_context import production_proxy_context
    context = production_proxy_context(context)
    assert context["pending_operational_parameter"] == "namespace"
    assert context["pending_operational_scope"] == "namespace"
    second = TestClient(application).post("/api/v1/chat", json={
        "message": "test-yapayzekarag", "conversation_context": context,
    })
    assert second.status_code == 200
    assert "egress-ai" in second.json()["answer"]
    assert "pending_operational_intent" not in second.json()["conversation_context"]
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.MCPClient")
def test_pending_operation_survives_cluster_as_a_separate_turn(
    mcp_class: Mock,
) -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"content": [{"type": "text", "text": json.dumps({
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "test-yapayzekarag", "labels": {"team": "ai"},
            },
        })}]},
        {"structuredContent": {"items": [_egress("egress-ai")]}},
    ]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    client = TestClient(application)

    cluster_question = client.post("/api/v1/chat", json={"message": "egressip"})
    context = cluster_question.json()["conversation_context"]
    assert context["pending_operational_parameter"] == "cluster"
    namespace_question = client.post("/api/v1/chat", json={
        "message": "KKB TEST", "conversation_context": context,
    })
    assert namespace_question.json()["answer"] == (
        "EgressIP sorgusu için namespace adını belirtin."
    )
    assert namespace_question.json()["conversation_context"][
        "pending_operational_parameter"
    ] == "namespace"
    answer = client.post("/api/v1/chat", json={
        "message": "test-yapayzekarag",
        "conversation_context": namespace_question.json()["conversation_context"],
    })
    assert answer.status_code == 200
    assert "egress-ai" in answer.json()["answer"]
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.MCPClient")
def test_namespace_egressip_scope_survives_later_cluster_answer(
    mcp_class: Mock,
) -> None:
    mcp = _egress_mcp("test-yapayzekarag", "10.60.1.222")
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    client = TestClient(application)
    first = client.post("/api/v1/chat", json={
        "message": "test-yapayzekarag namespace'inin egress ip'si nedir",
    })
    context = first.json()["conversation_context"]
    assert context["pending_operational_parameter"] == "cluster"
    assert context["pending_operational_namespace"] == "test-yapayzekarag"
    from tests.test_operational_context import production_proxy_context
    context = production_proxy_context(context)
    assert context["pending_operational_namespace"] == "test-yapayzekarag"
    second = client.post("/api/v1/chat", json={
        "message": "KKB TEST", "conversation_context": context,
    })
    assert second.status_code == 200
    assert "test-yapayzekarag" in second.json()["answer"]
    assert "10.60.1.222" in second.json()["answer"]
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.MCPClient")
def test_inventory_cluster_selection_and_pending_namespace_complete_end_to_end(
    mcp_class: Mock,
) -> None:
    from tests.test_operational_context import production_proxy_context

    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"content": [{"type": "text", "text": "NAME AGE"}],
         "structuredContent": {"items": [_egress("egress-ai")]}},
        {"structuredContent": {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-yapayzekarag", "labels": {"team": "ai"}},
        }]}},
        {"structuredContent": {"items": [_egress("egress-ai")]}},
    ]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    client = TestClient(application)

    inventory_message = "bünyendeki tüm egress ip'leri sıralar mısın"
    clarification = client.post("/api/v1/chat", json={"message": inventory_message})
    assert clarification.json()["needs_cluster_selection"] is True
    inventory = client.post("/api/v1/chat", json={
        "message": inventory_message, "target_cluster_ids": ["kkbtest"],
    })
    assert inventory.status_code == 200
    assert "egress-ai" in inventory.json()["answer"]
    assert mcp.call_tool.call_args_list == [call(
        "resources_list", {"apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP"},
    )]

    missing_cluster = client.post("/api/v1/chat", json={"message": "egressip"})
    assert missing_cluster.json()["needs_cluster_selection"] is True
    missing_namespace = client.post("/api/v1/chat", json={
        "message": "egressip", "target_cluster_ids": ["kkbtest"],
    })
    pending = production_proxy_context(
        missing_namespace.json()["conversation_context"]
    )
    resumed = client.post("/api/v1/chat", json={
        "message": "test-yapayzekarag", "conversation_context": pending,
    })
    assert resumed.status_code == 200
    assert "egress-ai" in resumed.json()["answer"]
    assert "pending_operational_intent" not in resumed.json()["conversation_context"]
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.MCPClient")
def test_explicit_egressip_operations_replace_stale_pod_focus(
    mcp_class: Mock,
) -> None:
    stale = ConversationContext(
        active_cluster_ids=("kkbtest",),
        active_inspection=ActiveInspection(
            "pod_health", "Pod", "kkbtest", namespace="lab-sdlc",
        ),
        investigation_focus="lab-sdlc",
        previous_operational_intent="inspect_pods",
    )
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {
        "structuredContent": {"items": [_egress("egress-ai")]},
    }
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "bünyendeki tüm egress ipleri listele",
        "conversation_context": stale.public_dict(),
    })
    assert response.status_code == 200
    assert "egress-ai" in response.json()["answer"]
    assert "lab-sdlc" not in response.json()["answer"]
    assert response.json()["conversation_context"]["active_inspection"] is None
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.MCPClient")
def test_explicit_namespace_egressip_replaces_stale_namespace_focus(
    mcp_class: Mock,
) -> None:
    stale = ConversationContext(
        active_cluster_ids=("kkbtest",), investigation_focus="lab-sdlc",
        active_inspection=ActiveInspection(
            "pod_health", "Pod", "kkbtest", namespace="lab-sdlc",
        ),
    )
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"structuredContent": {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-yapayzekarag", "labels": {"team": "ai"}},
        }]}},
        {"structuredContent": {"items": [_egress("egress-ai")]}},
    ]
    mcp_class.return_value = mcp
    application = create_app(settings(token="token"))
    application.state.llm_client = Mock()
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "test-yapayzekarag namespace'indeki egress ip nedir",
        "conversation_context": stale.public_dict(),
    })
    assert response.status_code == 200
    assert "test-yapayzekarag" in response.json()["answer"]
    assert "lab-sdlc" not in response.json()["answer"]
    assert response.json()["conversation_context"]["investigation_focus"] == (
        "test-yapayzekarag"
    )
    application.state.llm_client.chat_completion.assert_not_called()


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_new_cluster_wide_pod_query_drops_previous_egressip_namespace(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("fresh pods", [], [])
    previous = ConversationContext(
        active_cluster_ids=("kkbtest",), investigation_focus="test-yapayzekarag",
        previous_operational_intent="inspect_resource",
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "problemli podları kontrol et",
            "conversation_context": previous.public_dict(),
        },
    )
    assert response.status_code == 200
    call_args = agent_class.return_value.run.call_args
    assert "test-yapayzekarag" not in call_args.args[0]
    assert call_args.kwargs.get("required_fresh_tool") is None
    assert response.json()["conversation_context"].get("investigation_focus") is None


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_cluster_switch_clears_pending_egressip(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("switched", [], [])
    pending = ConversationContext(
        active_cluster_ids=("kkbtest",),
    ).with_pending_operational("egressip_lookup", "kkbtest")
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "RMTEST",
            "conversation_context": pending.public_dict(),
        },
    )
    assert response.status_code == 200
    context = response.json()["conversation_context"]
    assert "pending_operational_intent" not in context
    assert context["active_cluster_ids"] == ["rmtest"]


def test_valid_egressip_without_optional_selectors_or_status_is_not_rejected() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.return_value = {"structuredContent": {"items": [{
        "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
        "metadata": {"name": "unassigned"},
        "spec": {"egressIPs": ["10.60.1.222"]},
    }]}}
    result = AgentLoop(
        settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST",
    ).run("bünyendeki tüm egress ipleri listele")
    assert "unassigned" in result.answer
    assert "10.60.1.222" in result.answer
    assert "Henüz bir node ataması" in result.answer
    assert mcp.call_tool.call_count == 1


def test_malformed_nested_egressip_json_is_reported_as_malformed() -> None:
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
    assert "egressip_result status=malformed" in stream.getvalue()


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
