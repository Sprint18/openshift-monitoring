from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.conversation import ConversationContext
from app.intent import interpret_intent, natural_namespace_intent
from app.k8s_client import KubernetesListResult
from app.main import create_app
from app.namespace_inventory import NamespaceQuery
from tests.test_ai_backend import settings


def inventory(*names: str) -> KubernetesListResult:
    return KubernetesListResult(tuple(
        {"metadata": {"name": name}, "status": {"phase": "Active"}}
        for name in names
    ), "complete", 1)


@patch("app.main.KubernetesAPIAdapter")
def test_typo_membership_confirmation_and_active_entity_round_trip(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "uat-zauh"
    )
    client = TestClient(create_app(settings(token=None)))
    listed = client.post("/api/v1/chat", json={
        "message": "KKBTEST'te uat ile başlayan namespace'leri listele",
    })
    typo = client.post("/api/v1/chat", json={
        "message": "uat-zauth bunların içinde mi",
        "conversation_context": listed.json()["conversation_context"],
    })
    assert "`uat-zauth` namespace bulunamadı" in typo.json()["answer"]
    assert "`uat-zauh`" in typo.json()["answer"]
    accepted = client.post("/api/v1/chat", json={
        "message": "evet",
        "conversation_context": typo.json()["conversation_context"],
    })
    accepted_context = accepted.json()["conversation_context"]
    assert accepted_context["active_entity_name"] == "uat-zauh"
    assert accepted_context["last_filter_type"] == "prefix"
    status = client.post("/api/v1/chat", json={
        "message": "durumu ne", "conversation_context": accepted_context,
    })
    assert "uat-zauh" in status.json()["answer"]
    assert "Active" in status.json()["answer"]
    assert "`uat`" not in status.json()["answer"]


@patch("app.main.KubernetesAPIAdapter")
def test_verified_membership_sets_exact_entity_and_preserves_prefix(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-zauh")
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_filter_type="prefix", last_filter_value="uat",
        last_query_operation="prefix_search", last_operation="list",
    )
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": "uat-zauh bunların içinde mi",
            "conversation_context": context.public_dict(),
        },
    )
    payload = response.json()
    assert payload["answer"] == "## Cluster: KKB TEST\n\nEvet, `uat-zauh` bu grupta yer alıyor."
    assert payload["conversation_context"]["active_entity_name"] == "uat-zauh"
    assert payload["conversation_context"]["last_filter_value"] == "uat"


@pytest.mark.parametrize(("message", "expected"), [
    ("uat ile başlayanları bi döksene", NamespaceQuery("prefix", "uat", True)),
    ("kkbtestte toplam kaç proje vardı", NamespaceQuery("total", "", False)),
    ("rmtestte sandboxla başlayanlar neler", NamespaceQuery("prefix", "sandbox", True)),
    ("abi uat-zauh ne alemde bi baksana", NamespaceQuery("exact", "uat-zauh", False)),
])
def test_natural_turkish_namespace_grammar(
    message: str, expected: NamespaceQuery,
) -> None:
    intent = natural_namespace_intent(message, ConversationContext())
    assert intent is not None
    assert intent.namespace_query(ConversationContext()) == expected


@pytest.mark.parametrize("message", [
    "ne durumda bu", "bu nasıl", "bunda bi sorun görüyor musun",
])
def test_natural_anaphora_prefers_active_entity(message: str) -> None:
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_filter_type="prefix", last_filter_value="uat",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
    )
    intent = natural_namespace_intent(message, context)
    assert intent is not None
    assert intent.namespace_query(context) == NamespaceQuery("exact", "uat-zauh")


def test_llm_nlu_is_tool_free_bounded_and_schema_validated() -> None:
    llm = Mock()
    llm.is_configured.return_value = True
    llm.chat_completion.return_value = {"content": json.dumps({
        "mode": "followup", "resource_kind": "Namespace",
        "operation": "status", "entity_text": None, "query_type": None,
        "query_value": None, "reference": "active_entity",
        "cluster_ref": None, "response_mode": "concise",
    })}
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), active_entity_kind="Namespace",
        active_entity_name="uat-zauh",
    )
    intent = interpret_intent(llm, "az önce bahsettiğimin hali nasıl", context)
    assert intent is not None
    assert intent.namespace_query(context) == NamespaceQuery("exact", "uat-zauh")
    assert llm.chat_completion.call_args.kwargs == {}
    prompt = llm.chat_completion.call_args.args[0]
    assert len(prompt) == 2
    assert "evidence" not in str(prompt).casefold()
    assert "mcp_url" not in str(prompt).casefold()

    llm.chat_completion.return_value = {"content": '{"mode":"operational","resource_kind":"Secret"}'}
    assert interpret_intent(llm, "ona bak", context) is None
    llm.chat_completion.return_value = {"content": json.dumps({
        "mode": "operational", "resource_kind": "Namespace",
        "operation": "exists", "entity_text": "invented-namespace",
        "reference": "none",
    })}
    assert interpret_intent(llm, "ona bak", context) is None


@patch("app.main.KubernetesAPIAdapter")
def test_grounding_firewall_keeps_resource_questions_out_of_smalltalk(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-zauh")
    application = create_app(settings(token="token"))
    llm = Mock()
    llm.is_configured.return_value = True
    llm.chat_completion.return_value = {"content": "uydurulmuş cevap"}
    application.state.llm_client = llm
    client = TestClient(application)
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
    )
    for message in (
        "uat-zauh var mı?", "bu namespace sağlıklı mı?", "durumu ne?",
    ):
        response = client.post("/api/v1/chat", json={
            "message": message, "conversation_context": context.public_dict(),
        })
        assert "uydurulmuş" not in response.text
        assert "uat-zauh" in response.text
    llm.chat_completion.assert_not_called()
