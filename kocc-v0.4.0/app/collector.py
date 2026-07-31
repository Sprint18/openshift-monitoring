from __future__ import annotations

from collections import defaultdict
from typing import Any

from kubernetes import client
from kubernetes.client import ApiClient

from app.resource_parser import cpu_to_millicores, memory_to_bytes

API_REQUEST_TIMEOUT = (5, 30)
TERMINAL_POD_PHASES = {"Succeeded", "Failed"}


class ClusterCollector:
    def __init__(self, api_client: ApiClient) -> None:
        self.core_api = client.CoreV1Api(api_client)
        self.custom_api = client.CustomObjectsApi(api_client)

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

        for pod in pods:
            phase_counts[pod.status.phase or "Unknown"] += 1

        return {
            "total": len(pods),
            "running": phase_counts["Running"],
            "pending": phase_counts["Pending"],
            "failed": phase_counts["Failed"],
            "succeeded": phase_counts["Succeeded"],
            "unknown": phase_counts["Unknown"],
            "phase_counts": dict(sorted(phase_counts.items())),
        }

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
                    missing += 1
                if "cpu" not in limits:
                    namespace_item["missing_cpu_limit"] += 1
                    missing += 1
                if "memory" not in requests:
                    namespace_item["missing_memory_request"] += 1
                    missing += 1
                if "memory" not in limits:
                    namespace_item["missing_memory_limit"] += 1
                    missing += 1

                if missing == 4:
                    namespace_item["completely_undefined"] += 1

        namespace_items = sorted(
            namespaces.values(),
            key=lambda item: item["namespace"],
        )

        return {
            "cluster": cluster,
            "namespaces": namespace_items,
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

        return {
            "version": self.get_cluster_version(),
            "nodes": self.get_node_summary(nodes),
            "pods": self.get_pod_summary(pods),
            "namespace_count": self.get_namespace_count(namespaces),
            "resources": self.get_resource_summary(
                nodes,
                pods,
                namespaces,
            ),
        }
