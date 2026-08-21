from datetime import datetime, timezone
import re
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import (
    DASHBOARD_CACHE_TTL_SECONDS,
    DIAGNOSTIC_CACHE_TTL_SECONDS,
    app,
    cached_dashboard_data,
    clear_dashboard_cache,
    collection_time_severity,
    executive_dashboard,
    format_istanbul_time,
    health_score,
    is_platform_namespace,
    missing_resources_page,
    namespace_request_distribution,
    positive_env_seconds,
    prepare_dashboard_data,
    resource_severity,
    top_resource_limits,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_dashboard_cache() -> None:
    clear_dashboard_cache()


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

    response = client.get("/health-overview?cluster=kkbtest")

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
    cpu_requests = top_resource_limits(
        namespaces, "cpu", 4000, rank_by="request"
    )

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
    assert [item["namespace"] for item in cpu_requests] == [
        "large-cpu", "large-memory", "small"
    ]
    assert cpu_requests[0]["capacity_percent"] == 10.0


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

    response = client.get("/health-overview?cluster=kkbtest")

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

    response = client.get("/resources?cluster=kkbtest")

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

    local_response = client.get("/resources?cluster=kkbtest")
    remote_response = client.get("/resources?cluster=rmtest")

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

    response = client.get("/resources?cluster=kkbtest")

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

    response = client.get("/resources?cluster=kkbtest")

    assert response.status_code == 200
    assert '<option value="15">15 sec</option>' in response.text
    assert 'id="namespace-search"' in response.text
    assert 'id="include-openshift-namespaces"' in response.text
    assert "activePopup: null" in response.text
    assert 'event.key === "Escape"' in response.text
    assert "!state.activePopup.contains(event.target)" in response.text
    assert "window.clearTimeout(state.refreshTimer)" in response.text
    assert "window.__koccDashboardInitialized" in response.text
    clear_dashboard_cache()
    health_response = client.get("/health-overview?cluster=kkbtest")
    assert "CPU requests: kapasitenin" in health_response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_namespace_resource_numeric_sort_contract_supports_mixed_units(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["resources"]["namespaces"] = [
        {"namespace": "ten-core", "cpu_request": 10_000, "memory_request": 10 * 1024**3},
        {"namespace": "milli", "cpu_request": 900, "memory_request": 50 * 1024**2},
        {"namespace": "one-core", "cpu_request": 1000, "memory_request": 1024**3},
        {"namespace": "zero", "cpu_request": 0, "memory_request": 0},
    ]
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/resources?cluster=kkbtest")

    assert response.status_code == 200
    cpu_values = [int(value) for value in re.findall(
        r'data-cpu-request="(\d+)"', response.text
    )]
    memory_values = [int(value) for value in re.findall(
        r'data-memory-request="(\d+)"', response.text
    )]
    assert sorted(cpu_values) == [0, 900, 1000, 10_000]
    assert sorted(cpu_values, reverse=True) == [10_000, 1000, 900, 0]
    assert sorted(memory_values) == [0, 50 * 1024**2, 1024**3, 10 * 1024**3]
    assert "Number(left) - Number(right)" in response.text
    assert "left.getAttribute(`data-${key}`)" in response.text
    assert 'data-sort="namespace" data-type="text"' in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_missing_resource_search_pagination_csv_and_single_flight_contract(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    records = [
        {
            "namespace": "app-a" if index < 60 else "app-b",
            "pod": f"pod-{index:03}",
            "container": "api",
            "cpu_request": False,
            "cpu_limit": True,
            "memory_request": True,
            "memory_limit": True,
            "missing_count": 1,
        }
        for index in range(73)
    ]
    summary = {
        "count": 73,
        "namespace_count": 2,
        "container_count": 73,
        "items": [],
        "more_count": 53,
        "records": records,
    }
    data["resources"]["missing_resources"] = {
        "application": summary,
        "all": summary,
    }
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/resources?cluster=rmtest")

    assert response.status_code == 200
    assert "missingPageSize: 50" in response.text
    assert "start + state.missingPageSize" in response.text
    assert "KoccMissingResources.buildMissingResourcesView" in response.text
    assert "getFilteredMissingRecords()" in response.text
    assert "export-missing-csv" in response.text
    assert "refreshInProgress: false" in response.text
    assert "if (state.refreshInProgress) return" in response.text
    assert "seconds === 15 && dashboardData.collectionDuration > 10" in response.text
    assert '"missingApplicationRecords"' in response.text
    assert "pod-000" in response.text
    assert 'data-initial-record-count="50"' in response.text
    assert response.text.count("data-missing-record") >= 50
    assert "missing_resources_initialized application=" in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_namespace_search_filters_rows_with_single_pipeline(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["resources"]["namespaces"] = [
        {"namespace": "Alpha-Team"},
        {"namespace": "beta-app"},
        {"namespace": "gamma"},
    ]
    collector_class.return_value.collect_dashboard.return_value = data

    response = client.get("/resources?cluster=kkbtest")

    assert response.status_code == 200
    assert "const searchQuery = search.value.trim().toLocaleLowerCase" in response.text
    assert "row.dataset.namespace" in response.text
    assert ".includes(searchQuery)" in response.text
    assert "state.namespacePage = 1" in response.text


def test_official_kkb_logo_is_served_locally() -> None:
    response = client.get("/static/kkb-turuncu-lacivert-logo.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_health_returns_200() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_diagnostics_loading_text_is_turkish() -> None:
    response = client.get("/diagnostics?cluster=kkbtest")
    assert response.status_code == 200
    assert "Sorunlu podlar yükleniyor..." in response.text


@patch("app.main.new_cluster_client")
def test_health_probe_does_not_call_cluster_api(
    new_cluster_client: Mock,
) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    new_cluster_client.assert_not_called()
    readiness = client.get("/ready")
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    new_cluster_client.assert_not_called()


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_diagnostics_pages_are_lazy_and_render(
    new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    listing = client.get("/diagnostics?cluster=kkbtest")
    detail = client.get(
        "/diagnostics/apps/api-123?cluster=kkbtest"
    )

    assert listing.status_code == 200
    assert "Problemli Podlar" in listing.text
    assert "/api/diagnostics/pods" in listing.text
    assert detail.status_code == 200
    assert "Muhtemel Neden" in detail.text
    new_cluster_client.assert_not_called()
    collector_class.assert_not_called()


def test_diagnostic_log_tail_validation() -> None:
    response = client.get(
        "/api/diagnostics/apps/api-123?cluster=kkbtest&tail=42"
    )
    assert response.status_code == 400


@patch("app.main.prepare_dashboard_data")
def test_cache_is_cluster_isolated(prepare_data: Mock) -> None:
    prepare_data.side_effect = [
        {"selected_cluster": "kkbtest"},
        {"selected_cluster": "rmtest"},
    ]

    first = cached_dashboard_data("kkbtest")
    second = cached_dashboard_data("kkbtest")
    remote = cached_dashboard_data("rmtest")

    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert remote["selected_cluster"] == "rmtest"
    assert prepare_data.call_count == 2


@patch("app.main.prepare_dashboard_data")
def test_cache_hit_and_miss_are_logged(
    prepare_data: Mock, caplog,
) -> None:
    prepare_data.return_value = {"snapshot": True}

    with caplog.at_level("INFO", logger="kocc.performance"):
        cached_dashboard_data("kkbtest")
        cached_dashboard_data("kkbtest")

    assert "op=cache.snapshot" in caplog.text
    assert "cache_hit=false" in caplog.text
    assert "cache_hit=true" in caplog.text


@patch("app.main.prepare_dashboard_data")
def test_cache_force_refresh_bypasses_fresh_snapshot(
    prepare_data: Mock, caplog,
) -> None:
    prepare_data.side_effect = [{"generation": 1}, {"generation": 2}]
    with caplog.at_level("INFO", logger="kocc"):
        assert cached_dashboard_data("kkbtest")["generation"] == 1
        assert cached_dashboard_data("kkbtest")["generation"] == 1
        assert cached_dashboard_data("kkbtest", force_refresh=True)["generation"] == 2
    assert prepare_data.call_count == 2
    assert "cache=MISS" in caplog.text
    assert "cache=HIT" in caplog.text
    assert "cache=BYPASS" in caplog.text
    assert "ttl_ms=60000" in caplog.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_overview_and_resources_share_cluster_snapshot(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    collector_class.return_value.collect_dashboard.return_value = dashboard_payload()
    assert client.get("/?cluster=kkbtest").status_code == 200
    assert client.get("/resources?cluster=kkbtest").status_code == 200
    collector_class.return_value.collect_dashboard.assert_called_once()


@patch("app.main.DASHBOARD_CACHE_TTL_SECONDS", 0)
@patch("app.main.prepare_dashboard_data")
def test_cache_expiry_recollects(prepare_data: Mock) -> None:
    prepare_data.side_effect = [{"generation": 1}, {"generation": 2}]

    assert cached_dashboard_data("kkbtest")["generation"] == 1
    assert cached_dashboard_data("kkbtest")["generation"] == 2
    assert prepare_data.call_count == 2


@patch("app.main.DASHBOARD_CACHE_TTL_SECONDS", 0)
@patch("app.main.prepare_dashboard_data")
def test_cache_serves_marked_stale_snapshot_after_failure(
    prepare_data: Mock,
) -> None:
    prepare_data.side_effect = [
        {"generation": 1},
        RuntimeError("temporary cluster failure"),
    ]

    cached_dashboard_data("kkbtest")
    stale = cached_dashboard_data("kkbtest")

    assert stale["generation"] == 1
    assert stale["cache"]["stale"] is True


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


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_optional_workload_api_isolated_failure(
    new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.get_workload_summary.side_effect = (
        PermissionError("forbidden")
    )

    response = client.get(
        "/api/workloads?cluster=kkbtest&namespace=empty"
    )

    assert response.status_code == 200
    assert response.json() == {"available": False, "items": []}
    new_cluster_client.return_value.close.assert_called_once_with()


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_pvc_api_formats_requested_capacity(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.get_pvc_summary.return_value = {
        "total": 1,
        "requested_capacity": 1024**3,
        "bound": 1,
        "pending": 0,
        "items": [{
            "namespace": "data",
            "name": "db",
            "requested_capacity": 1024**3,
            "status": "Bound",
            "storage_class": "fast",
        }],
    }

    response = client.get("/api/pvcs?cluster=kkbtest")

    assert response.status_code == 200
    assert response.json()["requested_capacity_text"] == "1 GiB"
    assert response.json()["items"][0]["requested_capacity_text"] == "1 GiB"


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_route_api_contract(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.get_route_summary.return_value = [{
        "namespace": "app",
        "name": "portal",
        "host": "portal.example",
        "service": "web",
    }]

    response = client.get("/api/routes?cluster=rmtest")

    assert response.status_code == 200
    assert response.json()["items"][0]["host"] == "portal.example"


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_template_contains_p3_p4_contracts(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector_class.return_value.collect_dashboard.return_value = (
        dashboard_payload()
    )

    pages = {
        "/resources": "namespace-drilldown",
        "/workloads": "Global Workload Search",
        "/storage": "PVC / Storage",
        "/routes": "Route Search",
        "/health-overview": "Node Detayları",
    }
    for route, expected in pages.items():
        clear_dashboard_cache()
        response = client.get(f"{route}?cluster=kkbtest")
        assert response.status_code == 200
        assert expected in response.text
        assert "dashboardTheme" in response.text
        assert "Compare Clusters" not in response.text
        assert "compare-clusters" not in response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_overview_is_compact_and_lazy_apis_are_not_called(
    _new_cluster_client: Mock,
    collector_class: Mock,
) -> None:
    collector = collector_class.return_value
    collector.collect_dashboard.return_value = dashboard_payload()

    response = client.get("/?cluster=kkbtest")

    assert response.status_code == 200
    assert "Executive Summary" in response.text
    assert "Request Dağılımı (Namespace Bazında)" in response.text
    assert "CPU Request" in response.text
    assert "Memory Request" in response.text
    assert "Top 10 Namespace by CPU Requests" in response.text
    assert "Resource Hotspots" in response.text
    assert "Executive Insights" in response.text
    assert "Namespace Resource Özeti" not in response.text
    assert "Missing Requests / Limits</h2>" not in response.text
    assert "Route Search" not in response.text
    assert 'href="/resources?cluster=kkbtest"' in response.text
    assert 'href="/workloads?cluster=kkbtest"' in response.text
    collector.get_workload_summary.assert_not_called()
    collector.get_pvc_summary.assert_not_called()
    collector.collect_dashboard.assert_called_once()


def request_namespace(name: str, cpu: int = 0, memory: int = 0) -> dict:
    return {
        "namespace": name,
        "cpu_request": cpu,
        "memory_request": memory,
    }


def test_namespace_distribution_groups_below_one_percent_and_keeps_exact_one() -> None:
    result = namespace_request_distribution([
        request_namespace("exact-one", cpu=100),
        request_namespace("small-a", cpu=40),
        request_namespace("small-b", cpu=50),
    ], "cpu", 10_000)

    assert result["slices"][0]["label"] == "exact-one"
    others = next(item for item in result["slices"] if item["kind"] == "others")
    assert others["value"] == 90
    assert others["grouped_count"] == 2
    assert result["unused"] == 9_810


def test_namespace_distribution_caps_named_slices_and_sums_others() -> None:
    namespaces = [
        request_namespace(f"ns-{index:02}", cpu=200 - index)
        for index in range(15)
    ]
    result = namespace_request_distribution(namespaces, "cpu", 10_000)
    named = [item for item in result["slices"] if item["kind"] == "namespace"]
    others = next(item for item in result["slices"] if item["kind"] == "others")

    assert len(named) == 10
    assert len(result["slices"]) <= 12
    assert others["grouped_count"] == 5
    assert others["value"] == sum(200 - index for index in range(10, 15))


def test_namespace_distribution_over_capacity_has_zero_unused() -> None:
    result = namespace_request_distribution([
        request_namespace("large", cpu=12_000),
        request_namespace("second", cpu=3_000),
    ], "cpu", 10_000)

    assert result["total_requests"] == 15_000
    assert result["request_ratio"] == 150
    assert result["unused"] == 0
    assert result["over_capacity"] is True
    assert not any(item["kind"] == "unused" for item in result["slices"])


def test_cpu_and_memory_distributions_use_capacity_based_shares() -> None:
    namespaces = [request_namespace("app", cpu=2_000, memory=20 * 1024**3)]
    cpu = namespace_request_distribution(namespaces, "cpu", 10_000)
    memory = namespace_request_distribution(namespaces, "memory", 100 * 1024**3)

    assert cpu["slices"][0]["capacity_percent"] == 20
    assert memory["slices"][0]["capacity_percent"] == 20
    assert cpu["concentration"] == "warning"
    critical = namespace_request_distribution([
        request_namespace("critical", cpu=3_000)
    ], "cpu", 10_000)
    assert critical["concentration"] == "critical"


def test_platform_namespace_classifier_is_centralized() -> None:
    assert is_platform_namespace("openshift-storage") is True
    assert is_platform_namespace("kube-system") is True
    assert is_platform_namespace("default") is True
    assert is_platform_namespace("istio-system") is True
    assert is_platform_namespace("dynatrace") is True
    assert is_platform_namespace("sandbox-app") is False


def test_executive_dashboard_calculations_and_deterministic_insights() -> None:
    gib = 1024 ** 3
    data = {
        "health": {"score": 72, "status": "Critical"},
        "pods": {"problem_count": 12},
        "cluster_operators": {
            "available": True, "healthy": 30, "degraded": 1,
            "unavailable": 0,
        },
        "restarts": {
            "crashloop_count": 4,
            "restart_by_namespace": {"sandbox-app": 40, "openshift-monitoring": 2},
            "crashloop_by_namespace": {"sandbox-app": 4},
        },
        "resources": {
            "cluster": {
                "cpu_capacity": 10_000, "cpu_request": 8_000,
                "cpu_limit": 18_000, "memory_capacity": 100 * gib,
                "memory_request": 70 * gib, "memory_limit": 130 * gib,
            },
            "missing_resources": {"application": {"count": 120}},
            "namespaces": [
                {
                    "namespace": "openshift-monitoring", "cpu_request": 3_000,
                    "cpu_limit": 4_000, "memory_request": 30 * gib,
                    "memory_limit": 40 * gib, "missing_cpu_request": 0,
                    "missing_cpu_limit": 0, "missing_memory_request": 0,
                    "missing_memory_limit": 0,
                },
                {
                    "namespace": "sandbox-app", "cpu_request": 4_000,
                    "cpu_limit": 14_000, "memory_request": 40 * gib,
                    "memory_limit": 90 * gib, "missing_cpu_request": 30,
                    "missing_cpu_limit": 30, "missing_memory_request": 30,
                    "missing_memory_limit": 30,
                },
                {
                    "namespace": "dynatrace", "cpu_request": 1_000,
                    "cpu_limit": 1_000, "memory_request": 0,
                    "memory_limit": 0, "missing_cpu_request": 0,
                    "missing_cpu_limit": 0, "missing_memory_request": 0,
                    "missing_memory_limit": 0,
                },
            ],
        },
    }

    executive = executive_dashboard(data)

    assert executive["cpu"]["platform_percent"] == 40
    assert executive["cpu"]["applications_percent"] == 40
    assert executive["cpu"]["unused_percent"] == 20
    assert executive["rankings"]["cpu_request"][0]["namespace"] == "sandbox-app"
    assert executive["gauges"]["cpu"]["risk"] == "critical"
    assert executive["hotspots"][2]["namespace"] == "sandbox-app"
    assert any("CPU limitleri" in item for item in executive["insights"])
    assert any(action["href"] == "/diagnostics" for action in executive["actions"])


@patch("app.main.cached_dashboard_data")
def test_lightweight_pages_do_not_collect_dashboard(cached_data: Mock) -> None:
    for route in ("/workloads", "/storage", "/routes", "/diagnostics"):
        response = client.get(f"{route}?cluster=kkbtest")
        assert response.status_code == 200
    cached_data.assert_not_called()


def test_missing_resources_canonical_search_and_filtered_pagination() -> None:
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not installed")
    script_path = Path(__file__).parents[1] / "app/static/missing_resources.js"
    records = [
        {"namespace": name, "pod": f"{name}-pod", "container": "app", "missing_count": 1}
        for name in (
            "dynatrace", "dynatrace", "sandbox-a", "sandbox-app",
            "openshift-monitoring",
        )
    ]
    javascript = (
        f"const m=require({json.dumps(str(script_path))});"
        f"const records={json.dumps(records)};"
        "const view=m.buildMissingResourcesView(records,{"
        "includeOpenShift:false,query:'sandbox-a',sortKey:'namespace',"
        "sortDirection:'asc',page:2,pageSize:1});"
        "console.log(JSON.stringify(view));"
    )
    completed = subprocess.run(
        [bun, "-e", javascript], check=True, capture_output=True, text=True
    )
    view = json.loads(completed.stdout)
    assert [item["namespace"] for item in view["records"]] == [
        "sandbox-a", "sandbox-app"
    ]
    assert view["total"] == 2
    assert view["page"] == 2
    assert view["pageRecords"][0]["namespace"] == "sandbox-app"


def test_missing_resources_open_shift_filter_combines_with_search() -> None:
    source = (Path(__file__).parents[1] / "app/static/missing_resources.js").read_text()
    template = (Path(__file__).parents[1] / "app/templates/index.html").read_text()
    assert 'startsWith("openshift-")' in source
    assert "includes(query)" in source
    assert "state.missingPage = 1" in template
    assert "MissingResources init failed:" in template


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_rendered_resources_javascript_parses(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not installed")
    collector_class.return_value.collect_dashboard.return_value = dashboard_payload()
    response = client.get("/resources?cluster=kkbtest")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", response.text, re.S)
    inline = next(script for script in scripts if "initializeDashboard" in script)
    completed = subprocess.run(
        [bun, "-e", f"new Function({json.dumps(inline)});"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_rendered_executive_javascript_parses(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not installed")
    collector_class.return_value.collect_dashboard.return_value = dashboard_payload()
    response = client.get("/?cluster=kkbtest")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", response.text, re.S)
    inline = next(script for script in scripts if "initializeDashboard" in script)
    completed = subprocess.run(
        [bun, "-e", f"new Function({json.dumps(inline)});"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "executiveTrend:${dashboardData.cluster}" in response.text
    assert 'data-request-toggle="cpu"' in response.text
    assert 'data-request-toggle="memory"' in response.text
    assert 'data-request-mode="cpu"' in response.text
    assert 'data-request-mode="memory"' in response.text
    assert "setRequestMode(\"cpu\")" in response.text
    assert '[data-theme="dark"] .namespace-pie::after' in response.text
    assert "/api/" not in inline.split("const requestToggleButtons", 1)[1].split(
        "const trendRoot", 1
    )[0]


def missing_record(namespace: str, missing_count: int = 1) -> dict:
    return {
        "namespace": namespace, "pod": f"{namespace}-pod",
        "container": "app", "cpu_request": False, "cpu_limit": True,
        "memory_request": True, "memory_limit": False,
        "missing_count": missing_count,
    }


def test_missing_resources_backend_filter_sort_and_pagination() -> None:
    records = [
        missing_record("conjur", 1), missing_record("dynatrace", 2),
        missing_record("sandbox-a", 3), missing_record("sandbox-b", 1),
        missing_record("sandbox-payment", 2),
        missing_record("openshift-monitoring", 9),
    ]
    result = missing_resources_page(
        records, q="SANDBOX", include_openshift=False,
        sort="missing_count", direction="desc", page=1, page_size=2,
    )
    assert [item["namespace"] for item in result["records"]] == [
        "sandbox-a", "sandbox-payment"
    ]
    assert result == {**result, "total": 3, "page": 1, "page_size": 2, "pages": 2}
    assert all(not item["namespace"].startswith("openshift-") for item in result["records"])


@patch("app.main.cached_dashboard_data")
def test_dedicated_missing_resources_api_and_csv_share_filter(
    cached_data: Mock,
) -> None:
    records = [missing_record("dynatrace"), missing_record("sandbox-a")]
    cached_data.return_value = {
        "resources": {"missing_resources": {"all": {"records": records}}}
    }
    response = client.get(
        "/api/resources/missing?cluster=rmtest&q=sandbox&page=1&page_size=50"
    )
    csv_response = client.get(
        "/api/resources/missing.csv?cluster=rmtest&q=sandbox"
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["records"][0]["namespace"] == "sandbox-a"
    assert "sandbox-a" in csv_response.text
    assert "dynatrace" not in csv_response.text


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_resources_server_side_fallback_search(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    data = dashboard_payload()
    records = [missing_record("conjur"), missing_record("sandbox-a")]
    summary = {"count": 2, "namespace_count": 2, "container_count": 2,
               "items": [], "more_count": 0, "records": records}
    data["resources"]["missing_resources"] = {"application": summary, "all": summary}
    collector_class.return_value.collect_dashboard.return_value = data
    response = client.get("/resources?cluster=kkbtest&missing_q=sandbox")
    tbody = response.text.split('id="missing-resource-body"', 1)[1].split("</tbody>", 1)[0]
    assert "sandbox-a" in tbody
    assert "conjur" not in tbody


def test_snapshot_ttl_environment_defaults(monkeypatch) -> None:
    monkeypatch.delenv("KOCC_TEST_TTL", raising=False)
    assert positive_env_seconds("KOCC_TEST_TTL", 60) == 60
    monkeypatch.setenv("KOCC_TEST_TTL", "90")
    assert positive_env_seconds("KOCC_TEST_TTL", 60) == 90
    assert DASHBOARD_CACHE_TTL_SECONDS > 0
    assert DIAGNOSTIC_CACHE_TTL_SECONDS > 0


def test_stale_ajax_response_guard() -> None:
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not installed")
    script_path = Path(__file__).parents[1] / "app/static/missing_resources.js"
    completed = subprocess.run(
        [bun, "-e", (
            f"const m=require({json.dumps(str(script_path))});"
            "console.log(m.isCurrentRequest(1,2),m.isCurrentRequest(2,2));"
        )], check=True, capture_output=True, text=True,
    )
    assert completed.stdout.strip() == "false true"


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_diagnostics_reuses_compact_dashboard_snapshot(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    data = dashboard_payload()
    data["pods"]["diagnostic_items"] = [{
        "namespace": "apps", "name": "api", "phase": "Running",
        "ready": 0, "total": 1, "restarts": 12,
        "reason": "CrashLoopBackOff", "node": "worker-0",
        "age_seconds": 100, "severity": "critical",
    }]
    collector = collector_class.return_value
    collector.collect_dashboard.return_value = data
    assert client.get("/?cluster=kkbtest").status_code == 200
    response = client.get("/api/diagnostics/pods?cluster=kkbtest")
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "api"
    collector.get_problem_pods.assert_not_called()


@patch("app.main.ClusterCollector")
@patch("app.main.new_cluster_client")
def test_diagnostics_collects_when_dashboard_snapshot_is_absent(
    _new_cluster_client: Mock, collector_class: Mock,
) -> None:
    collector_class.return_value.get_problem_pods.return_value = []
    response = client.get("/api/diagnostics/pods?cluster=rmtest")
    assert response.status_code == 200
    collector_class.return_value.get_problem_pods.assert_called_once()
