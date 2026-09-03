from __future__ import annotations

import logging
import io
from unittest.mock import Mock, patch
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.agent import AgentLoop, AgentResult
from app.clusters import cluster_registry, validated_cluster_selection
from app.egressip import selector_matches
from app.egressip import egressip_namespace
from app.main import create_app
from tests.test_ai_backend import settings


def test_cluster_selection_is_validated_deduplicated_and_registry_ordered() -> None:
    scope = validated_cluster_selection(
        ["rmtest", "kkbtest", "rmtest"], cluster_registry(settings())
    )
    assert scope is not None
    assert scope.kind == "multiple"
    assert scope.cluster_ids == ("kkbtest", "rmtest")


@patch("app.main.MCPClient")
def test_ambiguous_operational_request_returns_choices_without_mcp(
    mcp_class: Mock,
) -> None:
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "Degraded ClusterOperator var mı?",
            "context_cluster_id": "rmtest",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.pop("clarification_id"), str)
    assert payload == {
        "answer": "Bu sorguyu hangi cluster veya clusterlar için çalıştırayım?",
        "needs_cluster_selection": True,
        "cluster_choices": [
            {"id": "kkbtest", "name": "KKB TEST"},
            {"id": "rmtest", "name": "RMTEST"},
            ],
            "allow_all": True,
            "conversation_context": {
                "active_cluster_ids": [],
                "last_resource_kind": None,
                "last_namespace": None,
                "last_query_operation": None,
                "last_operation": None,
                "last_filter_type": None,
                "last_filter_value": None,
                "pending_suggestion_original": None,
                "pending_suggestion_name": None,
                "active_entity_kind": None,
                "active_entity_name": None,
                "active_inspection": None,
            },
        }
    mcp_class.assert_not_called()


@patch("app.main.MCPClient")
def test_cluster_independent_identity_and_help_do_not_use_mcp(
    mcp_class: Mock,
) -> None:
    client = TestClient(create_app(settings(token=None)))
    for message in ("sen kimsin", "ne yapabilirsin", "what can you do"):
        response = client.post("/api/v1/chat", json={"message": message})
        assert response.status_code == 200
        assert response.json()["clusters"] == []
        assert "read-only OpenShift operasyon asistanıyım" in response.json()["answer"]
        assert "KKB TEST'e bağlı" not in response.json()["answer"]
    mcp_class.assert_not_called()


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_all_intent_precedes_temporary_selection_and_answers_are_attributed(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.side_effect = [
        AgentResult("KKB sonucu", []), AgentResult("RM sonucu", []),
    ]
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "tüm clusterlarda pod durumunu kontrol et",
            "target_cluster_ids": ["rmtest"],
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "all"
    assert response.json()["clusters"] == [
        {"id": "kkbtest", "name": "KKB TEST"},
        {"id": "rmtest", "name": "RMTEST"},
    ]
    assert "## KKB TEST" in response.json()["answer"]
    assert "## RMTEST" in response.json()["answer"]
    assert mcp_class.call_count == 2


def test_selector_semantics_cover_kubernetes_operators() -> None:
    labels = {"environment": "test", "team": "payments"}
    assert selector_matches({"matchLabels": {"environment": "test"}}, labels)
    assert selector_matches({"matchExpressions": [
        {"key": "team", "operator": "In", "values": ["payments"]},
        {"key": "retired", "operator": "DoesNotExist"},
    ]}, labels)
    assert not selector_matches({"matchExpressions": [
        {"key": "environment", "operator": "NotIn", "values": ["test"]},
    ]}, labels)
    assert selector_matches({"matchExpressions": [
        {"key": "missing", "operator": "NotIn", "values": ["prod"]},
    ]}, labels)


def test_egressip_namespace_intent_forms_are_narrowly_parsed() -> None:
    assert egressip_namespace("test-webmethods-gw egressip nedir") == "test-webmethods-gw"
    assert egressip_namespace("test-webmethods-gw namespace egress ip") == "test-webmethods-gw"
    assert egressip_namespace("test-webmethods-gw'ye ait egressip nedir") == "test-webmethods-gw"
    assert egressip_namespace(
        "test-webmethods-gw namespace'inin EgressIP'i nedir?"
    ) == "test-webmethods-gw"
    assert egressip_namespace("hangi egress ip kullanıyor") is None


def _resource_tool() -> dict:
    return {
        "name": "resources_list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["apiVersion", "kind"],
            "additionalProperties": False,
        },
    }


def _resource_get_tool() -> dict:
    return {
        "name": "resources_get",
        "inputSchema": {
            "type": "object",
            "properties": {
                "apiVersion": {"type": "string"},
                "kind": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["apiVersion", "kind", "name"],
        },
    }


def _egress_mcp(namespace: str, ip: str) -> Mock:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": namespace, "labels": {"scope": namespace}},
        }]},
        {"items": [{
            "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
            "metadata": {"name": f"assignment-{namespace}"},
            "spec": {
                "namespaceSelector": {"matchLabels": {"scope": namespace}},
                "podSelector": {},
            },
            "status": {"items": [{"egressIP": ip}]},
        }]},
    ]
    return mcp


def test_egressip_uses_namespace_selector_and_status_assignment() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-payments", "labels": {"team": "payments"}},
        }]},
        {"items": [{
            "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
            "metadata": {"name": "egress-payments"},
            "spec": {
                "namespaceSelector": {"matchLabels": {"team": "payments"}},
                "podSelector": {"matchLabels": {"egress": "enabled"}},
            },
            "status": {"items": [{"egressIP": "10.60.1.10", "node": "worker-1"}]},
        }]},
    ]
    result = AgentLoop(
        settings(token="token"), Mock(), mcp, "kkbtest", "KKB TEST"
    ).run("test-payments'e ait EgressIP nedir?")
    assert "10.60.1.10" in result.answer
    assert "worker-1" in result.answer
    assert "podSelector" in result.answer
    assert mcp.call_tool.call_args_list[1].args == (
        "resources_list", {"apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP"}
    )
    assert all(call.args[0] != "resources_get" for call in mcp.call_tool.call_args_list)


def test_egressip_table_list_uses_get_for_full_runtime_fixture() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool(), _resource_get_tool()]
    mcp.call_tool.side_effect = [
        {"structuredContent": {"result": {"content": [{
            "type": "text", "text": json.dumps({
                "apiVersion": "v1", "kind": "Namespace",
                "metadata": {
                    "name": "test-webmethods-gw",
                    "labels": {"kubernetes.io/metadata.name": "test-webmethods-gw"},
                },
            }),
        }]}}},
        {"structuredContent": {"result": {"content": [{
            "type": "text", "text": (
                "k8s.ovn.org/v1 EgressIP egress-test-ibm-gw\n"
            ),
        }]}}},
        {"structuredContent": {"result": {"resource": {
            "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
            "metadata": {"name": "egress-test-ibm-gw"},
            "spec": {
                "namespaceSelector": {"matchLabels": {
                    "kubernetes.io/metadata.name": "test-webmethods-gw",
                }},
                "podSelector": {},
            },
            "status": {"items": [{
                "egressIP": "10.60.1.207",
                "node": "kkbocptest1-695gq-ai-worker-z8fr9",
            }]},
        }}}},
    ]
    result = AgentLoop(
        settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST"
    ).run("test-webmethods-gw namespace EgressIP nedir")
    assert "test-webmethods-gw" in result.answer
    assert "egress-test-ibm-gw" in result.answer
    assert "10.60.1.207" in result.answer
    assert "kkbocptest1-695gq-ai-worker-z8fr9" in result.answer
    assert [call.args[0] for call in mcp.call_tool.call_args_list] == [
        "resources_get", "resources_list", "resources_get",
    ]
    assert "podSelector" not in result.answer


def test_egressip_list_failure_is_verification_failure() -> None:
    from app.mcp_client import MCPUnavailable

    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-payments", "labels": {"scope": "test"}},
        }]},
        MCPUnavailable("private list failure"),
    ]
    result = AgentLoop(
        settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST"
    ).run("test-payments egressip nedir")
    assert "doğrulanamadı" in result.answer
    assert "private" not in result.answer


def test_egressip_get_fallback_respects_existing_tool_call_budget() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool(), _resource_get_tool()]
    mcp.call_tool.side_effect = [
        {
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-payments", "labels": {"scope": "test"}},
        },
        {"items": [{
            "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
            "metadata": {"name": f"candidate-{index}"},
        } for index in range(9)]},
    ]
    limited = replace(settings(token=None), agent_max_tool_calls=10)
    result = AgentLoop(
        limited, Mock(), mcp, "kkbtest", "KKB TEST"
    ).run("test-payments egressip nedir")
    assert "doğrulanamadı" in result.answer
    assert mcp.call_tool.call_count == 2


def test_multiple_matching_egressips_are_returned_and_assignments_deduplicated() -> None:
    mcp = Mock()
    mcp.list_tools.return_value = [_resource_tool()]
    mcp.call_tool.side_effect = [
        {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-payments", "labels": {"team": "payments"}},
        }]},
        {"items": [
            {
                "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
                "metadata": {"name": "egress-a"},
                "spec": {
                    "namespaceSelector": {"matchLabels": {"team": "payments"}},
                    "podSelector": {},
                },
                "status": {"items": [
                    {"egressIP": "10.60.1.10", "node": "worker-1"},
                    {"egressIP": "10.60.1.10", "node": "worker-1"},
                ]},
            },
            {
                "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
                "metadata": {"name": "egress-b"},
                "spec": {
                    "namespaceSelector": {"matchExpressions": [{
                        "key": "team", "operator": "Exists",
                    }]},
                    "podSelector": {},
                },
                "status": {"items": [{"egressIP": "10.60.1.11"}]},
            },
        ]},
    ]
    result = AgentLoop(
        settings(token=None), Mock(), mcp, "kkbtest", "KKB TEST"
    ).run("test-payments egressip nedir")
    assert "egress-a" in result.answer and "egress-b" in result.answer
    assert result.answer.count("10.60.1.10") == 1
    assert "10.60.1.11" in result.answer


def test_egressip_no_match_and_mcp_failure_never_fabricate_ip() -> None:
    no_match = Mock()
    no_match.list_tools.return_value = [_resource_tool()]
    no_match.call_tool.side_effect = [
        {"items": [{
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": "test-payments", "labels": {"scope": "test-payments"}},
        }]},
        {"items": [{
            "apiVersion": "k8s.ovn.org/v1", "kind": "EgressIP",
            "metadata": {"name": "unrelated-assignment"},
            "spec": {
                "namespaceSelector": {"matchLabels": {"scope": "other"}},
                "podSelector": {},
            },
            "status": {"items": [{"egressIP": "10.60.1.99"}]},
        }]},
    ]
    result = AgentLoop(
        settings(token="token"), Mock(), no_match, "kkbtest", "KKB TEST"
    ).run("test-payments egressip nedir")
    assert "bulunamadı" in result.answer
    assert "10.60.1.99" not in result.answer

    failed = Mock()
    failed.list_tools.return_value = [_resource_tool()]
    from app.mcp_client import MCPUnavailable
    failed.call_tool.side_effect = MCPUnavailable("private endpoint detail")
    unavailable = AgentLoop(
        settings(token="token"), Mock(), failed, "kkbtest", "KKB TEST"
    ).run("test-payments egressip nedir")
    assert "doğrulanamadı" in unavailable.answer
    assert "private endpoint detail" not in unavailable.answer


@patch("app.main.MCPClient")
def test_multi_cluster_egressip_runs_independently_with_attribution(
    mcp_class: Mock,
) -> None:
    mcp_class.side_effect = [
        _egress_mcp("test-payments", "10.60.1.10"),
        _egress_mcp("test-payments", "10.70.1.10"),
    ]
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": "KKBTEST ve RMTEST'te test-payments egressip nedir"
        },
    )
    assert response.status_code == 200
    assert "## KKB TEST" in response.json()["answer"]
    assert "10.60.1.10" in response.json()["answer"]
    assert "## RMTEST" in response.json()["answer"]
    assert "10.70.1.10" in response.json()["answer"]


def test_runtime_logger_has_one_owned_visible_handler() -> None:
    handlers = [
        item for item in logging.getLogger("kocc_ai").handlers
        if getattr(item, "_kocc_ai_runtime", False)
    ]
    assert len(handlers) == 1
    assert logging.getLogger("kocc_ai").level == logging.INFO
    assert logging.getLogger("kocc_ai").propagate is False


@patch("app.main.MCPClient")
def test_mcp_failure_log_and_response_are_safe(mcp_class: Mock) -> None:
    from app.mcp_client import MCPUnavailable

    mcp_class.return_value.list_tools.side_effect = MCPUnavailable(
        "https://private-mcp.example token=secret"
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("kocc_ai").addHandler(handler)
    try:
        response = TestClient(create_app(settings(token="token"))).post(
            "/api/v1/chat", json={
                "message": "KKBTEST ClusterOperator durumunu kontrol et"
            },
        )
    finally:
        logging.getLogger("kocc_ai").removeHandler(handler)
    logs = stream.getvalue()
    assert response.status_code == 503
    assert response.json()["error"] == "mcp_unavailable"
    assert "ai_chat_failure target_clusters=kkbtest reason=mcp_unavailable" in logs
    assert "private-mcp" not in logs + response.text
    assert "token=secret" not in logs + response.text
