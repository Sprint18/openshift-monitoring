from __future__ import annotations

from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.agent import AgentResult
from app.conversation import (
    MAX_HISTORY_CHARS, MAX_HISTORY_TURNS, ConversationContext, bounded_history,
    contextual_namespace_query,
)
from app.k8s_client import KubernetesListResult
from app.main import create_app
from app.namespace_inventory import NamespaceQuery
from tests.test_ai_backend import settings


def namespace(name: str, phase: str = "Active") -> dict:
    return {"metadata": {"name": name}, "status": {"phase": phase}}


def inventory(*names: str) -> KubernetesListResult:
    return KubernetesListResult(
        tuple(namespace(name) for name in names), "complete", 1,
    )


def context_from(response) -> dict:
    return response.json()["conversation_context"]


@patch("app.main.KubernetesAPIAdapter")
@patch("app.main.MCPClient")
def test_conversational_llm_receives_bounded_history_without_tools_or_retrieval(
    mcp_class: Mock, adapter_class: Mock,
) -> None:
    application = create_app(settings(token="token"))
    llm = Mock()
    llm.is_configured.return_value = True
    llm.chat_completion.return_value = {"content": "İyiyim Alkın 🙂 Sen nasılsın?"}
    application.state.llm_client = llm
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "nasılsın",
        "recent_turns": [
            {"role": "user", "content": "merhaba"},
            {"role": "assistant", "content": "Merhaba 👋"},
            {"role": "user", "content": "benim adım Alkın"},
        ],
    })
    assert response.status_code == 200
    assert response.json()["answer"] == "İyiyim Alkın 🙂 Sen nasılsın?"
    messages = llm.chat_completion.call_args.args[0]
    assert [item["role"] for item in messages] == [
        "system", "user", "assistant", "user", "user",
    ]
    assert "Alkın" in messages[-2]["content"]
    assert llm.chat_completion.call_args.kwargs == {}
    mcp_class.assert_not_called()
    adapter_class.assert_not_called()


def test_conversational_llm_failure_uses_safe_deterministic_fallback() -> None:
    application = create_app(settings(token="token"))
    llm = Mock()
    llm.is_configured.return_value = True
    from app.llm_client import LLMUnavailable
    llm.chat_completion.side_effect = LLMUnavailable("private")
    application.state.llm_client = llm
    response = TestClient(application).post(
        "/api/v1/chat", json={"message": "merhaba"}
    )
    assert response.status_code == 200
    assert "Merhaba" in response.json()["answer"]
    assert "private" not in response.text


def test_history_is_bounded_and_payload_shaped_evidence_is_rejected() -> None:
    turns = [
        {"role": "user", "content": "x" * 2000} for _ in range(20)
    ] + [{
        "role": "assistant", "content": "unsafe",
        "evidence": [{"raw": "secret"}],
    }]
    result = bounded_history(turns)
    assert len(result) <= MAX_HISTORY_TURNS
    assert sum(len(item.content) for item in result) <= MAX_HISTORY_CHARS
    assert all(item.content != "unsafe" for item in result)
    context = ConversationContext.from_payload({
        "active_cluster_ids": [{"raw": "unsafe"}, "kkbtest", "unknown"],
        "last_resource_kind": {"raw": "unsafe"},
    })
    assert context.active_cluster_ids == ("kkbtest",)
    assert context.last_resource_kind is None


def test_contextual_followup_mapping_reuses_only_safe_namespace_semantics() -> None:
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_query_operation="prefix_search", last_filter_type="prefix",
        last_filter_value="uat",
    )
    assert contextual_namespace_query(
        "bu namespace'leri listele", context
    ) == NamespaceQuery("prefix", "uat", True)
    assert contextual_namespace_query(
        "kaç tanesi var?", context
    ) == NamespaceQuery("prefix", "uat", False)


@patch("app.main.KubernetesAPIAdapter")
def test_prefix_count_then_list_followup_reuses_cluster_and_live_inventory(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "uat-b", "x-uat"
    )
    client = TestClient(create_app(settings(token=None)))
    first = client.post("/api/v1/chat", json={
        "message": "KKBTEST ortamında uat ile başlayan kaç namespace var",
    })
    second = client.post("/api/v1/chat", json={
        "message": "bu namespace'leri listele",
        "conversation_context": context_from(first),
    })
    assert first.status_code == second.status_code == 200
    assert "**2**" in first.json()["answer"]
    assert "`uat-a`" in second.json()["answer"]
    assert "`uat-b`" in second.json()["answer"]
    assert "x-uat" not in second.json()["answer"]
    assert second.json()["cluster"] == "kkbtest"
    assert adapter_class.return_value.k8s_list.call_count == 2


@patch("app.main.KubernetesAPIAdapter")
def test_combined_count_and_list_is_canonical_and_ignores_old_assistant_claims(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "uat-b", "x-uat"
    )
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={
            "message": (
                "KKBTEST ortamında uat ile başlayan kaç namespace var "
                "bunları listele"
            ),
            "recent_turns": [{
                "role": "assistant", "content": "Daha önce 999 namespace vardı.",
            }],
        },
    )
    assert response.status_code == 200
    assert "**2**" in response.json()["answer"]
    assert "`uat-a`" in response.json()["answer"]
    assert "999" not in response.json()["answer"]


@patch("app.main.KubernetesAPIAdapter")
def test_exact_namespace_status_followup_performs_fresh_retrieval(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-zauh")
    client = TestClient(create_app(settings(token=None)))
    first = client.post("/api/v1/chat", json={
        "message": "KKBTEST'te uat-zauh var mı?",
    })
    second = client.post("/api/v1/chat", json={
        "message": "durumu ne?", "conversation_context": context_from(first),
    })
    assert "Active" in second.json()["answer"]
    assert adapter_class.return_value.k8s_list.call_count == 2


@patch("app.main.KubernetesAPIAdapter")
def test_typo_confirmation_precedes_smalltalk_and_never_silently_corrects(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-zauh")
    client = TestClient(create_app(settings(token=None)))
    typo = client.post("/api/v1/chat", json={
        "message": "KKBTEST clusterında uat-zaut namespace'i var mı",
    })
    assert "bulunamadı" in typo.json()["answer"]
    assert "uat-zauh" in typo.json()["answer"]
    accepted = client.post("/api/v1/chat", json={
        "message": "evet", "conversation_context": context_from(typo),
    })
    assert accepted.status_code == 200
    assert "baz alıyorum" in accepted.json()["answer"]
    assert "uat-zauh" in accepted.json()["answer"]
    assert "Active" in accepted.json()["answer"]


@patch("app.main.KubernetesAPIAdapter")
def test_typo_rejection_and_unrelated_turn_clear_pending_suggestion(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-zauh")
    client = TestClient(create_app(settings(token=None)))
    typo = client.post("/api/v1/chat", json={
        "message": "KKBTEST clusterında uat-zaut namespace'i var mı",
    })
    rejected = client.post("/api/v1/chat", json={
        "message": "hayır", "conversation_context": context_from(typo),
    })
    assert "kullanmayacağım" in rejected.json()["answer"]
    assert rejected.json()["conversation_context"]["pending_suggestion_name"] is None
    unrelated = client.post("/api/v1/chat", json={
        "message": "teşekkürler", "conversation_context": context_from(typo),
    })
    assert unrelated.json()["conversation_context"]["pending_suggestion_name"] is None


@patch("app.main.AgentLoop")
@patch("app.main.MCPClient")
def test_explicit_rmtest_followup_overrides_kkbtest_context_without_direct_api(
    mcp_class: Mock, agent_class: Mock,
) -> None:
    agent_class.return_value.run.return_value = AgentResult("partial", [], [])
    context = ConversationContext(
        active_cluster_ids=("kkbtest",), last_resource_kind="Namespace",
        last_query_operation="prefix_search", last_filter_type="prefix",
        last_filter_value="uat",
    )
    response = TestClient(create_app(settings(token="token"))).post(
        "/api/v1/chat", json={
            "message": "peki RMTEST?",
            "conversation_context": context.public_dict(),
        },
    )
    assert response.status_code == 200
    assert response.json()["cluster"] == "rmtest"
    assert mcp_class.call_args.args[0] == "https://rm-mcp.example/mcp"
    assert "uat ile başlayan" in agent_class.return_value.run.call_args.args[0]
    assert response.json()["conversation_context"]["active_cluster_ids"] == [
        "rmtest"
    ]


def test_no_pending_context_means_confirmation_is_ordinary_conversation() -> None:
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat", json={"message": "evet"}
    )
    assert response.status_code == 200
    assert response.json()["clusters"] == []
