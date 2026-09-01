from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.k8s_client import KubernetesListResult
from app.main import create_app
from app.namespace_inventory import NamespaceQuery, parse_namespace_query
from tests.test_ai_backend import settings


def namespace(name: str, phase: str = "Active") -> dict:
    return {"metadata": {"name": name}, "status": {"phase": phase}}


def inventory(*names: str) -> KubernetesListResult:
    return KubernetesListResult(
        tuple(namespace(name) for name in names), "complete", 1,
    )


@pytest.mark.parametrize(("message", "expected"), [
    ("kaç tane namespace var", NamespaceQuery("total", "")),
    ("toplam kaç namespace var", NamespaceQuery("total", "")),
    ("namespace sayısı nedir", NamespaceQuery("total", "")),
    ("uat ile başlayan kaç namespace var", NamespaceQuery("prefix", "uat")),
    ("uat içeren namespace'leri listele", NamespaceQuery("contains", "uat", True)),
    ("uat-zauh namespace var mı", NamespaceQuery("exact", "uat-zauh")),
    ("namespace uat-zauh mevcut mu", NamespaceQuery("exact", "uat-zauh")),
    ("uat-zauh projesinin durumu ne", NamespaceQuery("exact", "uat-zauh")),
])
def test_namespace_modes_are_distinct(message: str, expected: NamespaceQuery) -> None:
    assert parse_namespace_query(message) == expected


@pytest.mark.parametrize("message", [
    "tane namespace var", "namespace'leri listele",
    "bu namespace'leri listele", "hangi namespace'ler active",
])
def test_grammar_and_demonstratives_are_not_exact_namespace_names(
    message: str,
) -> None:
    assert parse_namespace_query(message) is None


@patch("app.main.KubernetesAPIAdapter")
def test_total_namespace_count_is_live_deterministic_and_returns_context(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "test-a", "openshift-monitoring"
    )
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat",
        json={"message": "KKBTEST ortamında kaç tane namespace var"},
    )
    assert response.status_code == 200
    assert "toplam **3** namespace var" in response.json()["answer"]
    assert "`tane`" not in response.json()["answer"]
    context = response.json()["conversation_context"]
    assert context["active_cluster_ids"] == ["kkbtest"]
    assert context["last_resource_kind"] == "Namespace"
    assert context["last_filter_type"] == "total"
    assert context["last_operation"] == "count"


@patch("app.main.KubernetesAPIAdapter")
def test_namespace_count_language_and_number_cannot_be_changed_by_llm(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-a", "uat-b")
    application = create_app(settings(token="token"))
    llm = Mock()
    llm.is_configured.return_value = True
    llm.chat_completion.return_value = {
        "content": "prefix_count operation sonucu 999 canonical fact"
    }
    application.state.llm_client = llm
    response = TestClient(application).post("/api/v1/chat", json={
        "message": "KKBTEST'te uat ile başlayan kaç namespace var",
    })
    answer = response.json()["answer"]
    assert '"uat" ile başlayan **2** namespace var' in answer
    assert "prefix_count" not in answer
    assert "operation" not in answer
    assert "canonical" not in answer
    assert "999" not in answer
    llm.chat_completion.assert_not_called()


@patch("app.main.KubernetesAPIAdapter")
def test_prefix_context_round_trip_changes_only_count_to_list(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory(
        "uat-a", "uat-b", "x-uat"
    )
    client = TestClient(create_app(settings(token=None)))
    first = client.post("/api/v1/chat", json={
        "message": "KKBTEST ortamında uat ile başlayan kaç namespace var",
    })
    context = first.json()["conversation_context"]
    assert context["last_filter_type"] == "prefix"
    assert context["last_filter_value"] == "uat"
    assert context["last_operation"] == "count"
    second = client.post("/api/v1/chat", json={
        "message": "bu namespace'leri listele",
        "conversation_context": context,
    })
    assert second.status_code == 200
    assert second.json().get("needs_cluster_selection") is not True
    assert "`uat-a`" in second.json()["answer"]
    assert "x-uat" not in second.json()["answer"]
    assert second.json()["conversation_context"]["last_operation"] == "list"


@patch("app.main.KubernetesAPIAdapter")
def test_cluster_clarification_preserves_original_prefix_list_query(
    adapter_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = inventory("uat-a", "test-a")
    client = TestClient(create_app(settings(token=None)))
    clarification = client.post("/api/v1/chat", json={
        "message": "uat ile başlayan namespace'leri listele",
    })
    assert clarification.json()["needs_cluster_selection"] is True
    pending = clarification.json()["conversation_context"]
    assert pending["last_filter_type"] == "prefix"
    assert pending["last_filter_value"] == "uat"
    assert pending["last_operation"] == "list"
    resolved = client.post("/api/v1/chat", json={
        "message": "uat ile başlayan namespace'leri listele",
        "target_cluster_ids": ["kkbtest"],
        "conversation_context": pending,
    })
    assert resolved.status_code == 200
    assert "`uat-a`" in resolved.json()["answer"]
    assert "`bu`" not in resolved.json()["answer"]


def test_rmtest_direct_api_remains_disabled() -> None:
    application = create_app(settings())
    assert application.state.clusters["rmtest"].kubernetes_api.enabled is False
