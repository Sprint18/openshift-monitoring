from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import app, prepare_dashboard_data

client = TestClient(app)


def dashboard_payload() -> dict:
    return {
        "version": "4.18.0",
        "nodes": {
            "total": 1,
            "ready": 1,
            "not_ready": 0,
            "role_counts": {"master": 1},
            "items": [
                {
                    "name": "master-0",
                    "roles": ["master"],
                    "ready": True,
                    "cpu_capacity": 4000,
                }
            ],
        },
        "pods": {"total": 2, "running": 2},
        "namespace_count": 1,
        "resources": {
            "cluster": {"cpu_capacity": 4000},
            "namespaces": [{"namespace": "default"}],
        },
    }


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_summary_contract_handles_missing_optional_metrics(
    new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.collect_dashboard.return_value = (
        dashboard_payload()
    )

    data = prepare_dashboard_data("kkbtest")

    assert data["selected_cluster"] == "kkbtest"
    assert data["nodes"]["items"][0]["memory_capacity"] == 0
    assert data["nodes"]["items"][0]["memory_capacity_text"] == "0 B"
    assert data["nodes"]["role_counts"] == {
        "master": 1,
        "infra": 0,
        "worker": 0,
        "other": 0,
    }
    assert data["pods"]["pending"] == 0
    assert data["resources"]["cluster"]["memory_request"] == 0
    assert data["resources"]["namespaces"][0]["cpu_request"] == 0
    new_cluster_client.return_value.close.assert_called_once_with()


@patch("app.main.prepare_dashboard_data")
def test_dashboard_renders_nodes_items_key(prepare_data: Mock) -> None:
    data = dashboard_payload()
    data["selected_cluster_name"] = "KKBTEST"
    data["nodes"]["role_counts"].update(
        {"infra": 0, "worker": 0, "other": 0}
    )
    data["pods"].update({"pending": 0, "failed": 0})
    cluster = data["resources"]["cluster"]
    for key in (
        "cpu_capacity_text",
        "cpu_allocatable_text",
        "cpu_request_text",
        "cpu_limit_text",
        "memory_capacity_text",
        "memory_allocatable_text",
        "memory_request_text",
        "memory_limit_text",
        "storage_capacity_text",
        "storage_allocatable_text",
    ):
        cluster[key] = "0"
    for key in (
        "cpu_request_percent",
        "cpu_limit_percent",
        "memory_request_percent",
        "memory_limit_percent",
    ):
        cluster[key] = 0
    node = data["nodes"]["items"][0]
    for key in (
        "cpu_capacity_text",
        "cpu_allocatable_text",
        "memory_capacity_text",
        "memory_allocatable_text",
        "storage_capacity_text",
        "storage_allocatable_text",
    ):
        node[key] = "0"
    namespace = data["resources"]["namespaces"][0]
    namespace.update(
        {
            "pod_count": 0,
            "container_count": 0,
            "cpu_request_text": "0",
            "cpu_limit_text": "0",
            "memory_request_text": "0",
            "memory_limit_text": "0",
            "missing_cpu_request": 0,
            "missing_cpu_limit": 0,
            "missing_memory_request": 0,
            "missing_memory_limit": 0,
            "completely_undefined": 0,
        }
    )
    prepare_data.return_value = data

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert "master-0" in response.text
    assert "OpenShift Clusters Monitoring Platform" in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_dashboard_renders_empty_node_list(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["nodes"] = {"items": []}
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert "Node Detayları" in response.text


def test_health_returns_200() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@patch("app.main.prepare_dashboard_data")
def test_api_summary_returns_dashboard_contract(prepare_data: Mock) -> None:
    prepare_data.return_value = dashboard_payload()

    response = client.get("/api/summary?cluster=kkbtest")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "4.18.0"
    assert isinstance(body["nodes"]["items"], list)
    assert "pods" in body
    assert "resources" in body


def test_unknown_cluster_returns_400() -> None:
    response = client.get("/api/summary?cluster=unknown")

    assert response.status_code == 400


@patch("app.main.new_cluster_client")
def test_unexpected_errors_do_not_leak_details(new_cluster_client: Mock) -> None:
    new_cluster_client.side_effect = RuntimeError("sensitive internal detail")

    response = client.get("/api/summary?cluster=kkbtest")

    assert response.status_code == 500
    assert "sensitive internal detail" not in response.text
