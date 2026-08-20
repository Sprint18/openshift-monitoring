from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import (
    app,
    collection_time_severity,
    format_istanbul_time,
    health_score,
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
    assert data["cluster_operators"] == {
        "available": False,
        "healthy": 0,
        "degraded": 0,
        "progressing": 0,
        "unavailable": 0,
        "items": [],
    }
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


def test_health_score_is_explainable_and_classified() -> None:
    data = dashboard_payload()
    data["nodes"].update({"total": 10, "not_ready": 1})
    data["pods"].update(
        {"total": 100, "succeeded": 10, "problem_count": 9}
    )
    data["resources"]["cluster"].update(
        {
            "cpu_capacity": 10_000,
            "cpu_request": 9000,
            "cpu_limit": 12_000,
            "memory_capacity": 1000,
            "memory_request": 700,
            "memory_limit": 900,
        }
    )
    data["cluster_operators"] = {
        "available": True,
        "degraded": 1,
        "progressing": 0,
        "unavailable": 0,
    }

    result = health_score(data)

    assert result["score"] == 82
    assert result["status"] == "Warning"
    assert result["node_penalty"] == 3
    assert result["pod_penalty"] == 2
    assert result["resource_penalty"] == 10
    assert result["operator_penalty"] == 3


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


def test_top_resource_limits_supports_5_10_and_20() -> None:
    namespaces = [
        {
            "namespace": f"ns-{index:02}",
            "cpu_request": index,
            "cpu_limit": index,
            "memory_request": index,
            "memory_limit": index,
        }
        for index in range(1, 26)
    ]

    for size in (5, 10, 20):
        result = top_resource_limits(
            namespaces, "cpu", 1000, limit=size
        )
        assert len(result) == size
        assert result[0]["namespace"] == "ns-25"
        assert result[-1]["rank"] == size


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
    assert '<option value="">All Namespaces</option>' in response.text
    assert 'value="namespace-049"' in response.text
    assert 'data-namespace="namespace-025"' in response.text
    assert "Critical CPU Limit" in response.text
    assert "Critical Memory Limit" in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_namespace_combobox_rebuilds_from_selected_cluster_data(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    kkbtest = dashboard_payload()
    kkbtest["resources"]["namespaces"] = [
        {"namespace": "zeta"},
        {"namespace": "alpha"},
        {"namespace": "beta"},
    ]
    rmtest = dashboard_payload()
    rmtest["resources"]["namespaces"] = [{"namespace": "remote-only"}]
    collector_class.return_value.collect_dashboard.side_effect = [
        kkbtest,
        rmtest,
    ]

    local_response = client.get("/?cluster=kkbtest")
    remote_response = client.get("/?cluster=rmtest")

    assert local_response.status_code == 200
    assert local_response.text.index('value="alpha"') < local_response.text.index(
        'value="beta"'
    ) < local_response.text.index('value="zeta"')
    assert 'value="remote-only"' not in local_response.text
    assert remote_response.status_code == 200
    assert 'value="remote-only"' in remote_response.text
    assert 'value="alpha"' not in remote_response.text
    assert "some((option) => option.value === previousValue)" in (
        remote_response.text
    )


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_namespace_combobox_handles_empty_namespace_list(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["namespace_count"] = 0
    data["resources"]["namespaces"] = []
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    select = response.text.split('<select id="namespace-filter"', 1)[1].split(
        "</select>", 1
    )[0]
    assert select.count("<option") == 1
    assert "All Namespaces" in select
    assert "Namespace verisi yok." in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_overcommit_calculation_uses_capacity(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["resources"]["cluster"].update(
        {
            "cpu_capacity": 4000,
            "cpu_limit": 6000,
            "memory_capacity": 8 * 1024**3,
            "memory_limit": 4 * 1024**3,
        }
    )
    collector_class.return_value.collect_dashboard.return_value = data

    result = prepare_dashboard_data("kkbtest")["resources"]["cluster"]

    assert result["cpu_limit_percent"] == 150.0
    assert result["cpu_overcommit_ratio"] == 1.5
    assert result["cpu_overcommitted"] is True
    assert result["memory_limit_percent"] == 50.0
    assert result["memory_overcommitted"] is False


def test_istanbul_timestamp_formatting_is_explicit() -> None:
    utc_time = datetime(2026, 1, 15, 7, 47, 31, tzinfo=timezone.utc)

    assert format_istanbul_time(utc_time) == "10:47:31"


def test_collection_time_severity_thresholds() -> None:
    assert collection_time_severity(4.99) == "normal"
    assert collection_time_severity(5) == "warning"
    assert collection_time_severity(9.99) == "warning"
    assert collection_time_severity(10) == "slow"


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_template_contains_popup_refresh_search_and_toggle_contract(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.collect_dashboard.return_value = (
        dashboard_payload()
    )

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert '<option value="15">15 sec</option>' in response.text
    assert 'id="namespace-search"' in response.text
    assert 'id="include-openshift-namespaces"' in response.text
    assert "activePopup: null" in response.text
    assert 'event.key === "Escape"' in response.text
    assert "!state.activePopup.contains(event.target)" in response.text
    assert "window.clearTimeout(state.refreshTimer)" in response.text
    assert "window.__koccDashboardInitialized" in response.text
    assert "CPU requests: kapasitenin" in response.text


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
