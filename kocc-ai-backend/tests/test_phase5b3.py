from __future__ import annotations

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.conversation import (
    MAX_HISTORY_CHARS, ConversationContext, bounded_history,
    contextual_entity_message, safe_conversation_summary,
)
from app.k8s_client import KubernetesListResult
from app.main import create_app
from tests.test_ai_backend import settings


def inventory(*names: str) -> KubernetesListResult:
    return KubernetesListResult(tuple(
        {"metadata": {"name": name}, "status": {"phase": "Active"}}
        for name in names
    ), "complete", 1)


@patch("app.main.KubernetesAPIAdapter")
def test_prefix_query_and_exact_active_entity_have_separate_memory(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "uat-zauh", "uat-webmethods-gw"
    )
    client = TestClient(create_app(settings(token=None)))
    listed = client.post("/api/v1/chat", json={
        "message": "kkbtest ortamında uat ile başlayan namespace'leri listele"
    })
    listed_context = listed.json()["conversation_context"]
    assert listed_context["last_filter_type"] == "prefix"
    assert listed_context["last_filter_value"] == "uat"
    assert listed_context["active_entity_name"] is None

    exact = client.post("/api/v1/chat", json={
        "message": "uat-zauh var mı bunların içinde",
        "conversation_context": listed_context,
    })
    exact_context = exact.json()["conversation_context"]
    assert exact_context["active_entity_kind"] == "Namespace"
    assert exact_context["active_entity_name"] == "uat-zauh"
    assert exact_context["last_filter_type"] == "prefix"
    assert exact_context["last_filter_value"] == "uat"

    status = client.post("/api/v1/chat", json={
        "message": "durumu ne", "conversation_context": exact_context,
    })
    assert "uat-zauh" in status.json()["answer"]
    assert "Active" in status.json()["answer"]


def test_active_entity_drives_related_followups_and_new_chat_is_empty() -> None:
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_filter_type="prefix", last_filter_value="uat",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
    )
    assert contextual_entity_message("peki bunun podları nasıl?", context) == (
        "uat-zauh namespace podları nasıl"
    )
    assert ConversationContext().active_entity_name is None


@patch("app.main.KubernetesAPIAdapter")
def test_explicit_new_namespace_replaces_active_entity(adapter_class: Mock) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-webmethods-gw")
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
    )
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": "uat-webmethods-gw tarafına bakalım",
            "conversation_context": context.public_dict(),
        },
    )
    assert response.json()["conversation_context"]["active_entity_name"] == (
        "uat-webmethods-gw"
    )


def test_long_context_is_bounded_and_summary_is_non_authoritative() -> None:
    turns = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 1500}
        for index in range(40)
    ] + [{"role": "assistant", "content": "raw", "tool_calls": [{}]}]
    bounded = bounded_history(turns)
    assert sum(len(turn.content) for turn in bounded) <= MAX_HISTORY_CHARS
    assert all(turn.content != "raw" for turn in bounded)
    assert len(bounded) > 10
    assert len(safe_conversation_summary("  önceki konu   uat-zauh  " * 200)) <= 1200


@patch("app.main.KubernetesAPIAdapter")
def test_casual_turn_does_not_clear_active_entity(adapter_class: Mock) -> None:
    application = create_app(settings(token=None))
    client = TestClient(application)
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        active_entity_kind="Namespace", active_entity_name="uat-zauh",
    )
    response = client.post("/api/v1/chat", json={
        "message": "teşekkürler", "conversation_context": context.public_dict(),
    })
    assert response.json()["conversation_context"]["active_entity_name"] == "uat-zauh"
    adapter_class.assert_not_called()
