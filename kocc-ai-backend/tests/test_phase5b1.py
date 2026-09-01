from __future__ import annotations

import json
import urllib.error
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.k8s_client import KubernetesAPIAdapter, KubernetesListResult
from app.k8s_policy import KubernetesPolicyViolation, validate_k8s_request
from app.main import create_app
from app.namespace_inventory import (
    NamespaceQuery, execute_namespace_query, namespace_suggestions,
    parse_namespace_query,
)
from tests.test_ai_backend import settings


class Response:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def adapter(tmp_path, **limits) -> KubernetesAPIAdapter:
    token = tmp_path / "token"
    ca = tmp_path / "ca.crt"
    token.write_text("private-token")
    ca.write_text("private-ca")
    return KubernetesAPIAdapter(
        cluster_id="kkbtest", timeout_seconds=2,
        page_limit=limits.get("page_limit", 2),
        max_pages=limits.get("max_pages", 5),
        max_items=limits.get("max_items", 20),
        token_path=str(token), ca_path=str(ca),
    )


def namespace(name: str, phase: str = "Active") -> dict:
    return {
        "metadata": {
            "name": name, "uid": "hidden", "managedFields": [{"x": "hidden"}],
            "annotations": {"secret-like": "hidden"},
        },
        "status": {"phase": phase, "other": "hidden"},
        "spec": {"finalizers": ["hidden"]},
    }


@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_namespace_list_paginates_and_projects_safe_fields(
    urlopen: Mock, context: Mock, tmp_path,
) -> None:
    urlopen.side_effect = [
        Response({"items": [namespace("uat-a"), namespace("x-uat")],
                  "metadata": {"continue": "next-token"}}),
        Response({"items": [namespace("uat-b", "Terminating")],
                  "metadata": {"continue": ""}}),
    ]
    result = adapter(tmp_path).k8s_list(api_version="v1", kind="Namespace")
    assert result.completeness == "complete"
    assert result.pages == 2
    assert result.items == (
        {"metadata": {"name": "uat-a"}, "status": {"phase": "Active"}},
        {"metadata": {"name": "x-uat"}, "status": {"phase": "Active"}},
        {"metadata": {"name": "uat-b"}, "status": {"phase": "Terminating"}},
    )
    assert "continue=next-token" in urlopen.call_args_list[1].args[0].full_url
    request = urlopen.call_args_list[0].args[0]
    assert request.get_header("Authorization") == "Bearer private-token"
    assert "private-token" not in request.full_url
    assert "managedFields" not in str(result)
    assert "annotations" not in str(result)
    context.assert_called_with(cafile=str(tmp_path / "ca.crt"))


@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_pagination_safety_cutoffs_are_partial(
    urlopen: Mock, _context: Mock, tmp_path,
) -> None:
    urlopen.return_value = Response({
        "items": [namespace("one")], "metadata": {"continue": "more"},
    })
    result = adapter(tmp_path, max_pages=1).k8s_list(
        api_version="v1", kind="Namespace"
    )
    assert result.completeness == "partial"
    assert result.warnings == ("max_pages_reached",)
    assert result.pages == 1


@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_exact_item_limit_on_final_page_remains_complete(
    urlopen: Mock, _context: Mock, tmp_path,
) -> None:
    urlopen.return_value = Response({
        "items": [namespace("one"), namespace("two")],
        "metadata": {"continue": ""},
    })
    result = adapter(tmp_path, max_items=2).k8s_list(
        api_version="v1", kind="Namespace"
    )
    assert result.completeness == "complete"
    assert len(result.items) == 2


@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_namespace_get_projects_safe_fields_and_handles_not_found(
    urlopen: Mock, _context: Mock, tmp_path,
) -> None:
    urlopen.side_effect = [
        Response(namespace("uat-a")),
        urllib.error.HTTPError(
            "https://kubernetes.default.svc/api/v1/namespaces/missing", 404,
            "Not Found", {}, None,
        ),
    ]
    found = adapter(tmp_path).k8s_get(
        api_version="v1", kind="Namespace", name="uat-a"
    )
    missing = adapter(tmp_path).k8s_get(
        api_version="v1", kind="Namespace", name="missing"
    )
    assert found.item == {
        "metadata": {"name": "uat-a"}, "status": {"phase": "Active"},
    }
    assert found.completeness == "complete"
    assert missing.item is None
    assert missing.completeness == "complete"


@pytest.mark.parametrize("failure", [TimeoutError("slow"), urllib.error.URLError("down")])
@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_transport_failure_is_unavailable_not_empty(
    urlopen: Mock, _context: Mock, tmp_path, failure: Exception,
) -> None:
    urlopen.side_effect = failure
    result = adapter(tmp_path).k8s_list(api_version="v1", kind="Namespace")
    assert result.completeness == "unavailable"
    assert result.items == ()
    assert result.warnings == ("request_failed",)


@patch("app.k8s_client.ssl.create_default_context")
@patch("app.k8s_client.urllib.request.urlopen")
def test_http_403_is_unavailable_not_empty_success(
    urlopen: Mock, _context: Mock, tmp_path,
) -> None:
    urlopen.side_effect = urllib.error.HTTPError(
        "https://kubernetes.default.svc/api/v1/namespaces", 403,
        "Forbidden", {}, None,
    )
    result = adapter(tmp_path).k8s_list(api_version="v1", kind="Namespace")
    assert result.completeness == "unavailable"
    assert result.warnings == ("http_403",)


@pytest.mark.parametrize(("operation", "api_version", "kind"), [
    ("list", "apps/v1", "Deployment"),
    ("list", "v1", "Secret"),
    ("create", "v1", "Namespace"),
    ("patch", "v1", "Namespace"),
    ("exec", "v1", "Namespace"),
])
def test_direct_api_policy_rejects_unsupported_sensitive_and_write_requests(
    operation: str, api_version: str, kind: str,
) -> None:
    with pytest.raises(KubernetesPolicyViolation):
        validate_k8s_request(
            operation=operation, api_version=api_version, kind=kind,
            name="example" if operation == "get" else None,
        )


def complete_result(*names: str) -> KubernetesListResult:
    return KubernetesListResult(
        tuple(namespace(name) for name in names), "complete", 2,
    )


def test_prefix_inventory_is_deterministic_and_excludes_contains_only_match() -> None:
    fake = Mock()
    fake.k8s_list.return_value = complete_result("uat-a", "x-uat", "UAT-b", "test-a")
    result = execute_namespace_query(
        fake, "kkbtest", NamespaceQuery("prefix", "uat", True)
    )
    assert "namespace sayısı: **2**" in result.answer
    assert "`uat-a`" in result.answer and "`UAT-b`" in result.answer
    assert "x-uat" not in result.answer
    envelope = result.evidence_items[0]
    assert envelope.source == "kubernetes_api"
    assert envelope.completeness == "complete"
    assert envelope.facts["matched_count"] == 2


@pytest.mark.parametrize(("message", "mode", "value"), [
    ("uat ile başlayan kaç namespace var", "prefix", "uat"),
    ("sandbox ile başlayan namespace'leri listele", "prefix", "sandbox"),
    ("uat içeren namespace'leri listele", "contains", "uat"),
    ("uat-zauh projesinde her şey yolunda mı", "exact", "uat-zauh"),
    ("uat-zauh namespace durumu nedir", "exact", "uat-zauh"),
    ("uat-zauh var mı", "exact", "uat-zauh"),
])
def test_project_and_namespace_wording_maps_to_inventory(
    message: str, mode: str, value: str,
) -> None:
    assert parse_namespace_query(message) == NamespaceQuery(
        mode, value, "listele" in message
    )


def test_generic_namespace_status_wording_is_not_misread_as_exact_name() -> None:
    assert parse_namespace_query("namespace status nedir") is None


def test_exact_namespace_and_conservative_did_you_mean() -> None:
    fake = Mock()
    fake.k8s_list.return_value = complete_result("uat-zauh", "uat-zakk", "test-a")
    exists = execute_namespace_query(fake, "kkbtest", NamespaceQuery("exact", "uat-zauh"))
    assert "namespace mevcut" in exists.answer and "Active" in exists.answer
    missing = execute_namespace_query(fake, "kkbtest", NamespaceQuery("exact", "uat-zaut"))
    assert "bulunamadı" in missing.answer
    assert "Şunu mu demek istediniz?" in missing.answer
    assert "uat-zauh" in missing.answer
    assert namespace_suggestions("uat-zaut", ["uat-zauh"], complete=False) == []


def test_partial_inventory_never_claims_exact_count_or_typo() -> None:
    fake = Mock()
    fake.k8s_list.return_value = KubernetesListResult(
        (namespace("uat-zauh"),), "partial", 1, ("max_pages_reached",),
    )
    result = execute_namespace_query(
        fake, "kkbtest", NamespaceQuery("prefix", "uat")
    )
    assert "kesin sonuç veremiyorum" in result.answer
    assert "sayısı:" not in result.answer
    assert "Şunu mu" not in result.answer
    assert result.evidence_items[0].completeness == "partial"


@patch("app.main.MCPClient")
@patch("app.main.KubernetesAPIAdapter")
def test_kkbtest_namespace_query_prefers_direct_api_without_mcp(
    adapter_class: Mock, mcp_class: Mock,
) -> None:
    adapter_class.return_value.k8s_list.return_value = complete_result(
        "uat-a", "uat-b", "test-a"
    )
    response = TestClient(create_app(settings(token=None))).post(
        "/api/v1/chat",
        json={"message": "KKBTEST'te uat ile başlayan kaç namespace var"},
    )
    assert response.status_code == 200
    assert '"uat" ile başlayan **2** namespace var' in response.json()["answer"]
    assert response.json()["cluster"] == "kkbtest"
    mcp_class.assert_not_called()


def test_registry_capability_is_private_and_kkbtest_only() -> None:
    application = create_app(settings())
    registry = application.state.clusters
    assert registry["kkbtest"].kubernetes_api.enabled is True
    assert registry["kkbtest"].kubernetes_api.transport == "in_cluster"
    assert registry["rmtest"].kubernetes_api.enabled is False
    response = TestClient(application).get("/api/v1/clusters")
    assert "kubernetes.default.svc" not in response.text
    assert "token" not in response.text.casefold()
