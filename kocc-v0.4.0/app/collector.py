from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from kubernetes import client
from kubernetes.client import ApiClient

from app.resource_parser import cpu_to_millicores, memory_to_bytes

API_REQUEST_TIMEOUT = (5, 30)
TERMINAL_POD_PHASES = {"Succeeded", "Failed"}
DETAIL_LIMIT = 20

logger = logging.getLogger("kocc.collector")


class ClusterCollector:
    def __init__(self, api_client: ApiClient) -> None:
        self.core_api = client.CoreV1Api(api_client)
        self.apps_api = client.AppsV1Api(api_client)
        self.custom_api = client.CustomObjectsApi(api_client)

    @staticmethod
    def workload_resources(containers: list[Any]) -> dict[str, int]:
        totals = {
            "cpu_request": 0,
            "cpu_limit": 0,
            "memory_request": 0,
            "memory_limit": 0,
        }
        for container_item in containers:
            resources = getattr(container_item, "resources", None)
            requests = getattr(resources, "requests", None) or {}
            limits = getattr(resources, "limits", None) or {}
            totals["cpu_request"] += cpu_to_millicores(requests.get("cpu"))
            totals["cpu_limit"] += cpu_to_millicores(limits.get("cpu"))
            totals["memory_request"] += memory_to_bytes(
                requests.get("memory")
            )
            totals["memory_limit"] += memory_to_bytes(limits.get("memory"))
        return totals

    def get_workload_summary(self, namespace: str | None = None) -> list[dict[str, Any]]:
        calls = (
            ("Deployment", self.apps_api.list_namespaced_deployment
             if namespace else self.apps_api.list_deployment_for_all_namespaces),
            ("StatefulSet", self.apps_api.list_namespaced_stateful_set
             if namespace else self.apps_api.list_stateful_set_for_all_namespaces),
            ("DaemonSet", self.apps_api.list_namespaced_daemon_set
             if namespace else self.apps_api.list_daemon_set_for_all_namespaces),
        )
        result: list[dict[str, Any]] = []
        for kind, method in calls:
            kwargs: dict[str, Any] = {"_request_timeout": API_REQUEST_TIMEOUT}
            if namespace:
                kwargs["namespace"] = namespace
            for workload in method(**kwargs).items:
                template = workload.spec.template.spec
                resources = self.workload_resources(
                    list(template.containers or [])
                    + list(template.init_containers or [])
                )
                desired = (
                    workload.status.desired_number_scheduled
                    if kind == "DaemonSet"
                    else workload.spec.replicas or 0
                )
                for key in resources:
                    resources[key] *= desired or 0
                ready = (
                    workload.status.number_ready
                    if kind == "DaemonSet"
                    else workload.status.ready_replicas or 0
                ) or 0
                available = getattr(
                    workload.status, "number_available", None
                )
                if available is None:
                    available = getattr(
                        workload.status, "available_replicas", 0
                    ) or 0
                result.append({
                    "type": kind,
                    "namespace": workload.metadata.namespace or namespace,
                    "name": workload.metadata.name,
                    "desired_replicas": desired or 0,
                    "ready_replicas": ready,
                    "available_replicas": available,
                    **resources,
                })
        return sorted(
            result,
            key=lambda item: (item["namespace"], item["type"], item["name"]),
        )

    def get_pvc_summary(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for pvc in self.core_api.list_persistent_volume_claim_for_all_namespaces(
            _request_timeout=API_REQUEST_TIMEOUT,
        ).items:
            requests = getattr(pvc.spec.resources, "requests", None) or {}
            items.append({
                "namespace": pvc.metadata.namespace or "default",
                "name": pvc.metadata.name,
                "requested_capacity": memory_to_bytes(requests.get("storage")),
                "status": pvc.status.phase or "Unknown",
                "storage_class": pvc.spec.storage_class_name or "N/A",
            })
        ranked = sorted(
            items,
            key=lambda item: (-item["requested_capacity"], item["namespace"], item["name"]),
        )
        return {
            "total": len(items),
            "requested_capacity": sum(item["requested_capacity"] for item in items),
            "bound": sum(item["status"] == "Bound" for item in items),
            "pending": sum(item["status"] == "Pending" for item in items),
            "items": ranked[:10],
        }

    def get_route_summary(self) -> list[dict[str, str]]:
        response = self.custom_api.list_cluster_custom_object(
            group="route.openshift.io",
            version="v1",
            plural="routes",
            _request_timeout=API_REQUEST_TIMEOUT,
        )
        routes = []
        for route in response.get("items", []):
            metadata = route.get("metadata", {})
            spec = route.get("spec", {})
            routes.append({
                "namespace": metadata.get("namespace", "default"),
                "name": metadata.get("name", "unknown"),
                "host": spec.get("host", ""),
                "service": spec.get("to", {}).get("name", ""),
            })
        return sorted(routes, key=lambda item: (item["namespace"], item["name"]))

    def get_cluster_version(self) -> str:
        result = self.custom_api.get_cluster_custom_object(
            group="config.openshift.io",
            version="v1",
            plural="clusterversions",
            name="version",
            _request_timeout=API_REQUEST_TIMEOUT,
        )
        status = result.get("status", {})
        history = status.get("history", [])

        if history:
            return history[0].get("version", "unknown")

        desired = status.get("desired", {})
        return desired.get("version", "unknown")

    @staticmethod
    def get_node_roles(labels: dict[str, str] | None) -> list[str]:
        labels = labels or {}
        prefix = "node-role.kubernetes.io/"
        roles = [
            key.removeprefix(prefix)
            for key in labels
            if key.startswith(prefix) and key.removeprefix(prefix)
        ]

        if not roles:
            return ["unknown"]

        role_order = {
            "master": 0,
            "control-plane": 0,
            "infra": 1,
            "worker": 2,
        }
        return sorted(
            set(roles),
            key=lambda role: (role_order.get(role, 99), role),
        )

    @staticmethod
    def is_node_ready(node: client.V1Node) -> bool:
        conditions = node.status.conditions or []
        return any(
            condition.type == "Ready" and condition.status == "True"
            for condition in conditions
        )

    def get_node_summary(
        self,
        nodes: list[client.V1Node] | None = None,
    ) -> dict[str, Any]:
        if nodes is None:
            nodes = self.core_api.list_node(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items
        role_counts = {
            "master": 0,
            "infra": 0,
            "worker": 0,
            "other": 0,
        }
        node_items: list[dict[str, Any]] = []
        ready_nodes = 0

        for node in nodes:
            ready = self.is_node_ready(node)
            if ready:
                ready_nodes += 1

            roles = self.get_node_roles(node.metadata.labels)
            if "master" in roles or "control-plane" in roles:
                primary_role = "master"
            elif "infra" in roles:
                primary_role = "infra"
            elif "worker" in roles:
                primary_role = "worker"
            else:
                primary_role = "other"

            role_counts[primary_role] += 1
            capacity = node.status.capacity or {}
            allocatable = node.status.allocatable or {}

            node_items.append(
                {
                    "name": node.metadata.name,
                    "roles": roles,
                    "primary_role": primary_role,
                    "ready": ready,
                    "cpu_capacity": cpu_to_millicores(capacity.get("cpu")),
                    "cpu_allocatable": cpu_to_millicores(
                        allocatable.get("cpu")
                    ),
                    "memory_capacity": memory_to_bytes(
                        capacity.get("memory")
                    ),
                    "memory_allocatable": memory_to_bytes(
                        allocatable.get("memory")
                    ),
                    "storage_capacity": memory_to_bytes(
                        capacity.get("ephemeral-storage")
                    ),
                    "storage_allocatable": memory_to_bytes(
                        allocatable.get("ephemeral-storage")
                    ),
                }
            )

        order = {"master": 0, "infra": 1, "worker": 2, "other": 3}
        node_items.sort(
            key=lambda item: (
                order.get(item["primary_role"], 99),
                item["name"],
            )
        )

        return {
            "total": len(nodes),
            "ready": ready_nodes,
            "not_ready": len(nodes) - ready_nodes,
            "role_counts": role_counts,
            "items": node_items,
        }

    def get_pod_summary(
        self,
        pods: list[client.V1Pod] | None = None,
    ) -> dict[str, Any]:
        if pods is None:
            pods = self.core_api.list_pod_for_all_namespaces(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items
        phase_counts: dict[str, int] = defaultdict(int)
        problem_items: list[dict[str, Any]] = []

        for pod in pods:
            phase = pod.status.phase or "Unknown"
            phase_counts[phase] += 1
            if phase == "Succeeded":
                continue

            statuses = getattr(pod.status, "container_statuses", None) or []
            total_containers = len(pod.spec.containers or [])
            ready_containers = sum(
                1 for status in statuses if status.ready
            )
            if phase == "Running" and ready_containers == total_containers:
                continue

            problem_items.append(
                {
                    "namespace": pod.metadata.namespace or "default",
                    "name": getattr(pod.metadata, "name", "unknown"),
                    "phase": phase,
                    "ready_containers": ready_containers,
                    "total_containers": total_containers,
                    "reason": self.pod_reason(pod),
                }
            )

        problem_items.sort(
            key=lambda item: (item["namespace"], item["name"])
        )
        problem_by_namespace: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in problem_items:
            if len(problem_by_namespace[item["namespace"]]) < 10:
                problem_by_namespace[item["namespace"]].append(item)

        return {
            "total": len(pods),
            "running": phase_counts["Running"],
            "pending": phase_counts["Pending"],
            "failed": phase_counts["Failed"],
            "succeeded": phase_counts["Succeeded"],
            "unknown": phase_counts["Unknown"],
            "phase_counts": dict(sorted(phase_counts.items())),
            "problem_count": len(problem_items),
            "problem_items": problem_items[:DETAIL_LIMIT],
            "problem_more_count": max(0, len(problem_items) - DETAIL_LIMIT),
            "problem_by_namespace": dict(problem_by_namespace),
        }

    @staticmethod
    def pod_reason(pod: client.V1Pod) -> str:
        statuses = list(
            getattr(pod.status, "init_container_statuses", None) or []
        )
        statuses.extend(
            getattr(pod.status, "container_statuses", None) or []
        )
        for status in statuses:
            state = getattr(status, "state", None)
            waiting = getattr(state, "waiting", None)
            if waiting and waiting.reason:
                return waiting.reason
            terminated = getattr(state, "terminated", None)
            if terminated and terminated.reason:
                return terminated.reason
        return (
            getattr(pod.status, "reason", None)
            or getattr(pod.status, "phase", None)
            or ""
        )

    def get_restart_summary(
        self,
        pods: list[client.V1Pod],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        crashloop_count = 0

        for pod in pods:
            if (pod.status.phase or "Unknown") == "Succeeded":
                continue
            statuses = list(
                getattr(pod.status, "init_container_statuses", None) or []
            )
            statuses.extend(
                getattr(pod.status, "container_statuses", None) or []
            )
            reasons = []
            for status in statuses:
                waiting = getattr(
                    getattr(status, "state", None), "waiting", None
                )
                if waiting and waiting.reason:
                    reasons.append(waiting.reason)
            if "CrashLoopBackOff" in reasons:
                crashloop_count += 1

            restart_count = sum(
                getattr(status, "restart_count", 0) or 0
                for status in statuses
            )
            if restart_count <= 0 and not reasons:
                continue
            items.append(
                {
                    "namespace": pod.metadata.namespace or "default",
                    "name": getattr(pod.metadata, "name", "unknown"),
                    "restart_count": restart_count,
                    "reason": reasons[0] if reasons else self.pod_reason(pod),
                }
            )

        items.sort(
            key=lambda item: (
                -item["restart_count"],
                item["namespace"],
                item["name"],
            )
        )
        by_namespace: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            if len(by_namespace[item["namespace"]]) < 10:
                by_namespace[item["namespace"]].append(item)
        return {
            "crashloop_count": crashloop_count,
            "items": items[:DETAIL_LIMIT],
            "by_namespace": dict(by_namespace),
        }

    def get_cluster_operator_summary(self) -> dict[str, Any]:
        try:
            result = self.custom_api.list_cluster_custom_object(
                group="config.openshift.io",
                version="v1",
                plural="clusteroperators",
                _request_timeout=API_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("ClusterOperator data unavailable: %s", exc)
            return {
                "available": False,
                "healthy": 0,
                "degraded": 0,
                "progressing": 0,
                "unavailable": 0,
                "items": [],
            }

        items: list[dict[str, Any]] = []
        counts = {
            "healthy": 0,
            "degraded": 0,
            "progressing": 0,
            "unavailable": 0,
        }
        for operator in result.get("items", []):
            conditions = {
                condition.get("type"): condition
                for condition in operator.get("status", {}).get(
                    "conditions", []
                )
            }
            available = conditions.get("Available", {}).get("status") == "True"
            progressing = (
                conditions.get("Progressing", {}).get("status") == "True"
            )
            degraded = conditions.get("Degraded", {}).get("status") == "True"
            if degraded:
                counts["degraded"] += 1
            if progressing:
                counts["progressing"] += 1
            if not available:
                counts["unavailable"] += 1
            if available and not progressing and not degraded:
                counts["healthy"] += 1

            relevant = (
                conditions.get("Degraded")
                if degraded
                else conditions.get("Progressing")
                if progressing
                else conditions.get("Available")
            ) or {}
            items.append(
                {
                    "name": operator.get("metadata", {}).get(
                        "name", "unknown"
                    ),
                    "available": available,
                    "progressing": progressing,
                    "degraded": degraded,
                    "reason": relevant.get("reason", ""),
                    "message": relevant.get("message", ""),
                }
            )

        items.sort(key=lambda item: item["name"])
        return {"available": True, **counts, "items": items}

    def get_namespace_count(
        self,
        namespaces: list[client.V1Namespace] | None = None,
    ) -> int:
        if namespaces is None:
            namespaces = self.core_api.list_namespace(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items
        return len(namespaces)

    def get_resource_summary(
        self,
        nodes: list[client.V1Node] | None = None,
        pods: list[client.V1Pod] | None = None,
        namespace_list: list[client.V1Namespace] | None = None,
    ) -> dict[str, Any]:
        if nodes is None:
            nodes = self.core_api.list_node(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items
        if pods is None:
            pods = self.core_api.list_pod_for_all_namespaces(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items
        if namespace_list is None:
            namespace_list = self.core_api.list_namespace(
                _request_timeout=API_REQUEST_TIMEOUT,
            ).items

        cluster = {
            "cpu_capacity": 0,
            "cpu_allocatable": 0,
            "cpu_request": 0,
            "cpu_limit": 0,
            "memory_capacity": 0,
            "memory_allocatable": 0,
            "memory_request": 0,
            "memory_limit": 0,
            "storage_capacity": 0,
            "storage_allocatable": 0,
        }

        for node in nodes:
            capacity = node.status.capacity or {}
            allocatable = node.status.allocatable or {}

            cluster["cpu_capacity"] += cpu_to_millicores(
                capacity.get("cpu")
            )
            cluster["cpu_allocatable"] += cpu_to_millicores(
                allocatable.get("cpu")
            )
            cluster["memory_capacity"] += memory_to_bytes(
                capacity.get("memory")
            )
            cluster["memory_allocatable"] += memory_to_bytes(
                allocatable.get("memory")
            )
            cluster["storage_capacity"] += memory_to_bytes(
                capacity.get("ephemeral-storage")
            )
            cluster["storage_allocatable"] += memory_to_bytes(
                allocatable.get("ephemeral-storage")
            )

        namespaces: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "namespace": "",
                "pod_count": 0,
                "container_count": 0,
                "cpu_request": 0,
                "cpu_limit": 0,
                "memory_request": 0,
                "memory_limit": 0,
                "missing_cpu_request": 0,
                "missing_cpu_limit": 0,
                "missing_memory_request": 0,
                "missing_memory_limit": 0,
                "completely_undefined": 0,
            }
        )
        missing_details: list[dict[str, str]] = []
        missing_records: list[dict[str, Any]] = []

        for namespace in namespace_list:
            namespace_name = namespace.metadata.name
            namespace_item = namespaces[namespace_name]
            namespace_item["namespace"] = namespace_name

        for pod in pods:
            if pod.status.phase in TERMINAL_POD_PHASES:
                continue

            namespace = pod.metadata.namespace or "default"
            namespace_item = namespaces[namespace]
            namespace_item["namespace"] = namespace
            namespace_item["pod_count"] += 1

            containers = list(pod.spec.containers or [])
            containers.extend(pod.spec.init_containers or [])

            for container_item in containers:
                namespace_item["container_count"] += 1
                resources = container_item.resources
                requests = getattr(resources, "requests", None) or {}
                limits = getattr(resources, "limits", None) or {}

                cpu_request = cpu_to_millicores(requests.get("cpu"))
                cpu_limit = cpu_to_millicores(limits.get("cpu"))
                memory_request = memory_to_bytes(
                    requests.get("memory")
                )
                memory_limit = memory_to_bytes(limits.get("memory"))

                namespace_item["cpu_request"] += cpu_request
                namespace_item["cpu_limit"] += cpu_limit
                namespace_item["memory_request"] += memory_request
                namespace_item["memory_limit"] += memory_limit

                cluster["cpu_request"] += cpu_request
                cluster["cpu_limit"] += cpu_limit
                cluster["memory_request"] += memory_request
                cluster["memory_limit"] += memory_limit

                missing = 0
                if "cpu" not in requests:
                    namespace_item["missing_cpu_request"] += 1
                    missing_details.append(
                        self.missing_detail(pod, container_item, "CPU Request")
                    )
                    missing += 1
                if "cpu" not in limits:
                    namespace_item["missing_cpu_limit"] += 1
                    missing_details.append(
                        self.missing_detail(pod, container_item, "CPU Limit")
                    )
                    missing += 1
                if "memory" not in requests:
                    namespace_item["missing_memory_request"] += 1
                    missing_details.append(
                        self.missing_detail(
                            pod, container_item, "Memory Request"
                        )
                    )
                    missing += 1
                if "memory" not in limits:
                    namespace_item["missing_memory_limit"] += 1
                    missing_details.append(
                        self.missing_detail(
                            pod, container_item, "Memory Limit"
                        )
                    )
                    missing += 1

                if missing == 4:
                    namespace_item["completely_undefined"] += 1

                if missing:
                    missing_records.append(
                        {
                            "namespace": namespace,
                            "pod": getattr(
                                pod.metadata, "name", "unknown"
                            ),
                            "container": getattr(
                                container_item, "name", "unknown"
                            ),
                            "cpu_request": "cpu" in requests,
                            "cpu_limit": "cpu" in limits,
                            "memory_request": "memory" in requests,
                            "memory_limit": "memory" in limits,
                            "missing_count": missing,
                        }
                    )

        namespace_items = sorted(
            namespaces.values(),
            key=lambda item: item["namespace"],
        )

        application_missing = self.summarize_missing_details(
            [
                item
                for item in missing_details
                if not item["namespace"].startswith("openshift-")
            ],
            [
                item
                for item in missing_records
                if not item["namespace"].startswith("openshift-")
            ],
        )
        all_missing = self.summarize_missing_details(
            missing_details,
            missing_records,
        )

        return {
            "cluster": cluster,
            "namespaces": namespace_items,
            "missing_resources": {
                "application": application_missing,
                "all": all_missing,
            },
            "missing_details": application_missing["items"],
            "missing_detail_count": application_missing["count"],
            "missing_detail_more_count": application_missing["more_count"],
        }

    @staticmethod
    def summarize_missing_details(
        details: list[dict[str, str]],
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "count": len(details),
            "namespace_count": len(
                {item["namespace"] for item in details}
            ),
            "container_count": len(records),
            "items": details[:DETAIL_LIMIT],
            "more_count": max(0, len(details) - DETAIL_LIMIT),
            "records": records,
        }

    @staticmethod
    def missing_detail(
        pod: client.V1Pod,
        container_item: client.V1Container,
        field: str,
    ) -> dict[str, str]:
        return {
            "namespace": pod.metadata.namespace or "default",
            "pod": getattr(pod.metadata, "name", "unknown"),
            "container": getattr(container_item, "name", "unknown"),
            "field": field,
        }

    def collect_dashboard(self) -> dict[str, Any]:
        nodes = self.core_api.list_node(
            _request_timeout=API_REQUEST_TIMEOUT,
        ).items
        pods = self.core_api.list_pod_for_all_namespaces(
            _request_timeout=API_REQUEST_TIMEOUT,
        ).items
        namespaces = self.core_api.list_namespace(
            _request_timeout=API_REQUEST_TIMEOUT,
        ).items

        resource_summary = self.get_resource_summary(
            nodes,
            pods,
            namespaces,
        )

        return {
            "version": self.get_cluster_version(),
            "nodes": self.get_node_summary(nodes),
            "pods": self.get_pod_summary(pods),
            "restarts": self.get_restart_summary(pods),
            "cluster_operators": self.get_cluster_operator_summary(),
            "namespace_count": self.get_namespace_count(namespaces),
            "resources": resource_summary,
            "search": {
                "pods": [
                    {
                        "type": "Pod",
                        "namespace": pod.metadata.namespace or "default",
                        "name": pod.metadata.name,
                    }
                    for pod in pods
                    if pod.status.phase != "Succeeded"
                ],
            },
        }
