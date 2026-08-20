from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import (
    app,
    prepare_dashboard_data,
    resource_severity,
    top_resource_limits,
)

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
    assert data["resources"]["namespace_options"] == ["default"]
    assert data["resources"]["top_limits"] == {"cpu": [], "memory": []}
    new_cluster_client.return_value.close.assert_called_once_with()


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_dashboard_renders_nodes_items_key(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.collect_dashboard.return_value = (
        dashboard_payload()
    )

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert "master-0" in response.text
    assert "OpenShift Clusters Monitoring Platform" in response.text


def test_resource_severity_uses_capacity_thresholds() -> None:
    assert resource_severity(999, 10_000) == "normal"
    assert resource_severity(1000, 10_000) == "warning"
    assert resource_severity(2000, 10_000) == "high"
    assert resource_severity(3000, 10_000) == "critical"
    assert resource_severity(1000, 0) == "normal"


def test_top_cpu_and_memory_limits_are_sorted() -> None:
    namespaces = [
        {
            "namespace": "small",
            "cpu_request": 100,
            "cpu_limit": 500,
            "memory_request": 1024,
            "memory_limit": 4096,
        },
        {
            "namespace": "large-memory",
            "cpu_request": 200,
            "cpu_limit": 1000,
            "memory_request": 2048,
            "memory_limit": 8192,
        },
        {
            "namespace": "large-cpu",
            "cpu_request": 400,
            "cpu_limit": 2000,
            "memory_request": 512,
            "memory_limit": 2048,
        },
    ]

    cpu = top_resource_limits(namespaces, "cpu", 4000)
    memory = top_resource_limits(namespaces, "memory", 16_384)

    assert [item["namespace"] for item in cpu] == [
        "large-cpu",
        "large-memory",
        "small",
    ]
    assert [item["namespace"] for item in memory] == [
        "large-memory",
        "small",
        "large-cpu",
    ]
    assert cpu[0]["capacity_percent"] == 50.0
    assert memory[0]["capacity_percent"] == 50.0


def test_top_resource_limits_handles_empty_namespaces() -> None:
    assert top_resource_limits([], "cpu", 4000) == []
    assert top_resource_limits([], "memory", 1024**3) == []


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


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_template_renders_many_namespaces_and_filter_contract(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["resources"]["cluster"].update(
        {"cpu_capacity": 10_000, "memory_capacity": 100 * 1024**3}
    )
    data["resources"]["namespaces"] = [
        {
            "namespace": f"namespace-{index:03}",
            "cpu_limit": index * 100,
            "memory_limit": index * 1024**3,
        }
        for index in range(50)
    ]
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert 'value="All Namespaces"' in response.text
    assert 'value="namespace-049"' in response.text
    assert 'data-namespace="namespace-025"' in response.text
    assert "Critical CPU Limit" in response.text
    assert "Critical Memory Limit" in response.text


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
