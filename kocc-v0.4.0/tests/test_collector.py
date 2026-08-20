from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.collector import API_REQUEST_TIMEOUT, ClusterCollector


def item_list(items: list[object]) -> SimpleNamespace:
    return SimpleNamespace(items=items)


def container(
    resources: object | None = None,
    name: str = "app",
) -> SimpleNamespace:
    return SimpleNamespace(resources=resources, name=name)


def container_status(
    ready: bool,
    restarts: int = 0,
    waiting_reason: str | None = None,
) -> SimpleNamespace:
    waiting = (
        SimpleNamespace(reason=waiting_reason)
        if waiting_reason
        else None
    )
    return SimpleNamespace(
        ready=ready,
        restart_count=restarts,
        state=SimpleNamespace(waiting=waiting, terminated=None),
    )


def workload(
    kind: str,
    name: str = "api",
    namespace: str = "app-ns",
) -> SimpleNamespace:
    status = SimpleNamespace(
        ready_replicas=2,
        available_replicas=2,
        desired_number_scheduled=3,
        number_ready=2,
        number_available=2,
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(
            replicas=3,
            template=SimpleNamespace(spec=SimpleNamespace(
                containers=[container(SimpleNamespace(
                    requests={"cpu": "250m", "memory": "128Mi"},
                    limits={"cpu": "1", "memory": "1Gi"},
                ))],
                init_containers=[],
            )),
        ),
        status=status,
        kind=kind,
    )
def pod(
    namespace: str,
    phase: str,
    containers: list[object],
    init_containers: list[object] | None = None,
    name: str = "pod-1",
    statuses: list[object] | None = None,
    init_statuses: list[object] | None = None,
    reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name),
        status=SimpleNamespace(
            phase=phase,
            reason=reason,
            container_statuses=statuses or [],
            init_container_statuses=init_statuses or [],
        ),
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
    assert result["missing_detail_count"] == 4
    assert result["missing_details"][0] == {
        "namespace": "active",
        "pod": "pod-1",
        "container": "app",
        "field": "CPU Request",
    }
    assert core_api_class.called


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_missing_resources_excludes_only_openshift_namespaces_by_default(
    _core_api_class: Mock,
    _custom_api_class: Mock,
) -> None:
    collector = ClusterCollector(Mock())
    pods = [
        pod("app-ns", "Running", [container()], name="app-pod"),
        pod(
            "openshift-monitoring",
            "Running",
            [container()],
            name="platform-pod",
        ),
    ]

    result = collector.get_resource_summary([], pods, [])
    missing = result["missing_resources"]

    assert missing["application"]["count"] == 4
    assert missing["application"]["namespace_count"] == 1
    assert missing["application"]["container_count"] == 1
    assert missing["application"]["items"][0]["namespace"] == "app-ns"
    assert missing["application"]["records"] == [
        {
            "namespace": "app-ns",
            "pod": "app-pod",
            "container": "app",
            "cpu_request": False,
            "cpu_limit": False,
            "memory_request": False,
            "memory_limit": False,
            "missing_count": 4,
        }
    ]
    assert missing["all"]["count"] == 8
    assert missing["all"]["namespace_count"] == 2
    assert missing["all"]["container_count"] == 2
    assert len(missing["all"]["records"]) == 2


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_pod_summary_detects_non_ready_and_ignores_succeeded(
    _core_api_class: Mock,
    _custom_api_class: Mock,
) -> None:
    collector = ClusterCollector(Mock())
    pods = [
        pod("ns", "Running", [container()], statuses=[container_status(True)]),
        pod(
            "ns",
            "Running",
            [container()],
            name="unready",
            statuses=[container_status(False)],
        ),
        pod("ns", "Pending", [container()], name="pending"),
        pod("ns", "Succeeded", [container()], name="completed"),
    ]

    result = collector.get_pod_summary(pods)

    assert result["problem_count"] == 2
    assert [item["name"] for item in result["problem_items"]] == [
        "pending",
        "unready",
    ]
    assert "completed" not in {
        item["name"] for item in result["problem_items"]
    }


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_pod_summary_reports_all_ready(
    _core_api_class: Mock,
    _custom_api_class: Mock,
) -> None:
    collector = ClusterCollector(Mock())
    healthy = pod(
        "ns",
        "Running",
        [container(), container(name="sidecar")],
        statuses=[container_status(True), container_status(True)],
    )

    result = collector.get_pod_summary([healthy])

    assert result["problem_count"] == 0
    assert result["problem_items"] == []


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_restart_summary_detects_crashloop_and_ranks_restarts(
    _core_api_class: Mock,
    _custom_api_class: Mock,
) -> None:
    collector = ClusterCollector(Mock())
    pods = [
        pod(
            "ns",
            "Running",
            [container()],
            name="crashing",
            statuses=[container_status(False, 8, "CrashLoopBackOff")],
        ),
        pod(
            "ns",
            "Running",
            [container()],
            name="restarted",
            statuses=[container_status(True, 3)],
        ),
        pod(
            "ns",
            "Succeeded",
            [container()],
            name="job",
            statuses=[container_status(False, 20)],
        ),
    ]

    result = collector.get_restart_summary(pods)
    pod_summary = collector.get_pod_summary(pods)

    assert result["crashloop_count"] == 1
    assert [item["name"] for item in result["items"]] == [
        "crashing",
        "restarted",
    ]
    assert result["items"][0]["reason"] == "CrashLoopBackOff"
    assert pod_summary["problem_items"][0]["reason"] == "CrashLoopBackOff"


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_cluster_operator_summary_degrades_when_api_is_unavailable(
    _core_api_class: Mock,
    custom_api_class: Mock,
) -> None:
    custom_api_class.return_value.list_cluster_custom_object.side_effect = (
        PermissionError("forbidden")
    )

    result = ClusterCollector(Mock()).get_cluster_operator_summary()

    assert result["available"] is False
    assert result["items"] == []


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_cluster_operator_summary_counts_conditions(
    _core_api_class: Mock,
    custom_api_class: Mock,
) -> None:
    custom_api_class.return_value.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "network"},
                "status": {
                    "conditions": [
                        {"type": "Available", "status": "False"},
                        {
                            "type": "Progressing",
                            "status": "True",
                            "reason": "Updating",
                        },
                        {"type": "Degraded", "status": "True"},
                    ]
                },
            }
        ]
    }

    result = ClusterCollector(Mock()).get_cluster_operator_summary()

    assert result["available"] is True
    assert result["degraded"] == 1
    assert result["progressing"] == 1
    assert result["unavailable"] == 1


@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_collect_dashboard_reuses_cluster_lists_and_sets_timeouts(
    core_api_class: Mock,
    custom_api_class: Mock,
    caplog: object,
) -> None:
    caplog.set_level("INFO", logger="kocc.collector")
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
    for step in (
        "collect_nodes",
        "collect_pods",
        "collect_namespaces",
        "resource_summary",
        "collect_operators",
        "collect_dashboard_total",
    ):
        assert step in caplog.text


@patch("app.collector.client.AppsV1Api")
@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_workload_summary_parses_replicas_and_resources(
    _core_api_class: Mock,
    _custom_api_class: Mock,
    apps_api_class: Mock,
) -> None:
    apps_api = apps_api_class.return_value
    apps_api.list_namespaced_deployment.return_value = item_list([
        workload("Deployment")
    ])
    apps_api.list_namespaced_stateful_set.return_value = item_list([])
    apps_api.list_namespaced_daemon_set.return_value = item_list([])

    result = ClusterCollector(Mock()).get_workload_summary("app-ns")

    assert result[0] == {
        "type": "Deployment",
        "namespace": "app-ns",
        "name": "api",
        "desired_replicas": 3,
        "ready_replicas": 2,
        "available_replicas": 2,
        "cpu_request": 750,
        "cpu_limit": 3000,
        "memory_request": 3 * 128 * 1024**2,
        "memory_limit": 3 * 1024**3,
    }


@patch("app.collector.client.AppsV1Api")
@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_pvc_and_route_parsing(
    core_api_class: Mock,
    custom_api_class: Mock,
    _apps_api_class: Mock,
) -> None:
    pvc = SimpleNamespace(
        metadata=SimpleNamespace(namespace="data", name="db"),
        spec=SimpleNamespace(
            resources=SimpleNamespace(requests={"storage": "20Gi"}),
            storage_class_name="fast",
        ),
        status=SimpleNamespace(phase="Bound"),
    )
    core_api_class.return_value.list_persistent_volume_claim_for_all_namespaces.return_value = item_list([pvc])
    custom_api_class.return_value.list_cluster_custom_object.return_value = {
        "items": [{
            "metadata": {"namespace": "app", "name": "portal"},
            "spec": {"host": "portal.example", "to": {"name": "web"}},
        }]
    }
    collector = ClusterCollector(Mock())

    pvc_result = collector.get_pvc_summary()
    routes = collector.get_route_summary()

    assert pvc_result["total"] == 1
    assert pvc_result["requested_capacity"] == 20 * 1024**3
    assert pvc_result["bound"] == 1
    assert routes == [{
        "namespace": "app",
        "name": "portal",
        "host": "portal.example",
        "service": "web",
        "tls": False,
        "status": "Unknown",
    }]


@patch("app.collector.client.AppsV1Api")
@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_diagnostics_excludes_succeeded_pods_and_handles_empty_events(
    core_api_class: Mock,
    _custom_api_class: Mock,
    _apps_api_class: Mock,
) -> None:
    completed = pod("jobs", "Succeeded", [container()], name="done")
    pending = pod("apps", "Pending", [container()], name="waiting")
    pending.metadata.uid = "uid-1"
    pending.metadata.creation_timestamp = None
    completed.metadata.uid = "uid-2"
    completed.metadata.creation_timestamp = None
    pending.spec.node_name = None
    completed.spec.node_name = None
    core_api = core_api_class.return_value
    core_api.list_pod_for_all_namespaces.return_value = item_list([
        completed, pending
    ])
    core_api.list_event_for_all_namespaces.return_value = item_list([])

    result = ClusterCollector(Mock()).get_problem_pods()

    assert [item["name"] for item in result] == ["waiting"]
    assert result[0]["reason"] == "Pending"


@patch("app.collector.client.AppsV1Api")
@patch("app.collector.client.CustomObjectsApi")
@patch("app.collector.client.CoreV1Api")
def test_pod_diagnostic_previous_log_is_optional(
    core_api_class: Mock,
    _custom_api_class: Mock,
    _apps_api_class: Mock,
) -> None:
    status = SimpleNamespace(
        name="api", ready=False, restart_count=2,
        state=SimpleNamespace(waiting=SimpleNamespace(
            reason="CrashLoopBackOff", message="back-off"
        ), terminated=None, running=None),
        last_state=SimpleNamespace(terminated=SimpleNamespace(
            reason="Error", exit_code=1, started_at=None, finished_at=None
        )),
    )
    diagnostic_pod = pod(
        "apps", "Running",
        [SimpleNamespace(
            name="api", image="registry/api:1",
            resources=SimpleNamespace(limits={"memory": "512Mi"}),
        )],
        name="crashing", statuses=[status],
    )
    diagnostic_pod.metadata.owner_references = []
    diagnostic_pod.spec.node_name = "worker-0"
    diagnostic_pod.status.message = None
    diagnostic_pod.status.pod_ip = "10.0.0.1"
    diagnostic_pod.status.start_time = None
    core_api = core_api_class.return_value
    core_api.read_namespaced_pod.return_value = diagnostic_pod
    core_api.list_namespaced_event.return_value = item_list([])
    core_api.read_namespaced_pod_log.side_effect = [
        "current log", RuntimeError("previous unavailable")
    ]

    result = ClusterCollector(Mock()).get_pod_diagnostic(
        "apps", "crashing", None, 200
    )

    assert result["events"] == []
    assert result["logs"]["current"] == "current log"
    assert result["logs"]["previous_available"] is False
    assert result["pod"]["containers"][0]["last_exit_code"] == 1
