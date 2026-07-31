from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.collector import API_REQUEST_TIMEOUT, ClusterCollector


def item_list(items: list[object]) -> SimpleNamespace:
    return SimpleNamespace(items=items)


def container(resources: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(resources=resources)


def pod(
    namespace: str,
    phase: str,
    containers: list[object],
    init_containers: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace),
        status=SimpleNamespace(phase=phase),
        spec=SimpleNamespace(
            containers=containers,
            init_containers=init_containers or [],
        ),
    )


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_resource_summary_handles_missing_resources_and_terminal_pods(
    core_api_class: Mock,
    _custom_api_class: Mock,
) -> None:
    collector = ClusterCollector(Mock())
    active = pod("active", "Running", [container()])
    completed = pod(
        "active",
        "Succeeded",
        [
            container(
                SimpleNamespace(
                    requests={"cpu": "5", "memory": "1Gi"},
                    limits={},
                )
            )
        ],
    )
    namespaces = [
        SimpleNamespace(metadata=SimpleNamespace(name="active")),
        SimpleNamespace(metadata=SimpleNamespace(name="empty")),
    ]

    result = collector.get_resource_summary([], [active, completed], namespaces)

    assert result["cluster"]["cpu_request"] == 0
    assert [item["namespace"] for item in result["namespaces"]] == [
        "active",
        "empty",
    ]
    assert result["namespaces"][0]["completely_undefined"] == 1
    assert core_api_class.called


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_collect_dashboard_reuses_cluster_lists_and_sets_timeouts(
    core_api_class: Mock,
    custom_api_class: Mock,
) -> None:
    core_api = core_api_class.return_value
    core_api.list_node.return_value = item_list([])
    core_api.list_pod_for_all_namespaces.return_value = item_list([])
    core_api.list_namespace.return_value = item_list([])
    custom_api_class.return_value.get_cluster_custom_object.return_value = {
        "status": {"desired": {"version": "4.18.0"}}
    }

    result = ClusterCollector(Mock()).collect_dashboard()

    assert result["version"] == "4.18.0"
    core_api.list_node.assert_called_once_with(
        _request_timeout=API_REQUEST_TIMEOUT
    )
    core_api.list_pod_for_all_namespaces.assert_called_once_with(
        _request_timeout=API_REQUEST_TIMEOUT
    )
    core_api.list_namespace.assert_called_once_with(
        _request_timeout=API_REQUEST_TIMEOUT
    )
