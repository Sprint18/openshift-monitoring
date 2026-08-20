from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from kubernetes.client.exceptions import ApiException

from app.cluster_loader import (
    DEFAULT_CLUSTER,
    ClusterConfigurationError,
    ClusterNotFoundError,
    get_cluster_definition,
    get_cluster_definitions,
    new_cluster_client,
)
from app.collector import ClusterCollector
from app.resource_parser import format_cpu, format_memory

logger = logging.getLogger("kocc")

app = FastAPI(
    title="OpenShift Clusters Monitoring Platform",
    version="0.4.0",
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "application": "OpenShift Clusters Monitoring Platform",
        "version": app.version,
    }


def percentage(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100, 2)


def resource_severity(value: int, capacity: int) -> str:
    ratio = percentage(value, capacity)
    if ratio >= 30:
        return "critical"
    if ratio >= 20:
        return "high"
    if ratio >= 10:
        return "warning"
    return "normal"


def health_score(data: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable 100-point score.

    Nodes account for 30 points, non-ready pods for 20, four resource
    pressure/overcommit signals for 40, and ClusterOperators for 10. Missing
    optional ClusterOperator data is neutral instead of penalizing the cluster.
    """
    nodes = data["nodes"]
    pods = data["pods"]
    cluster = data["resources"]["cluster"]
    operators = data["cluster_operators"]

    node_penalty = round(
        30 * nodes["not_ready"] / max(nodes["total"], 1)
    )
    active_pods = max(pods["total"] - pods["succeeded"], 1)
    pod_penalty = round(
        20 * min(pods["problem_count"], active_pods) / active_pods
    )

    ratios = {
        "cpu_request": percentage(
            cluster["cpu_request"], cluster["cpu_capacity"]
        ),
        "cpu_limit": percentage(
            cluster["cpu_limit"], cluster["cpu_capacity"]
        ),
        "memory_request": percentage(
            cluster["memory_request"], cluster["memory_capacity"]
        ),
        "memory_limit": percentage(
            cluster["memory_limit"], cluster["memory_capacity"]
        ),
    }

    def ratio_penalty(ratio: float, is_limit: bool) -> int:
        healthy_threshold = 100 if is_limit else 80
        critical_threshold = 150 if is_limit else 100
        if ratio <= healthy_threshold:
            return 0
        if ratio <= critical_threshold:
            return 5
        return 10

    resource_penalty = sum(
        ratio_penalty(ratio, key.endswith("limit"))
        for key, ratio in ratios.items()
    )
    operator_penalty = 0
    if operators["available"]:
        operator_penalty = min(
            10,
            operators["degraded"] * 3
            + operators["unavailable"] * 2
            + operators["progressing"],
        )

    score = max(
        0,
        100
        - node_penalty
        - pod_penalty
        - resource_penalty
        - operator_penalty,
    )
    status = "Healthy" if score >= 90 else "Warning" if score >= 75 else "Critical"
    return {
        "score": score,
        "status": status,
        "node_penalty": node_penalty,
        "pod_penalty": pod_penalty,
        "resource_penalty": resource_penalty,
        "operator_penalty": operator_penalty,
        "signals": ratios,
    }


def top_resource_limits(
    namespaces: list[dict[str, Any]],
    resource: str,
    capacity: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit_key = f"{resource}_limit"
    request_key = f"{resource}_request"
    ranked = sorted(
        (
            namespace
            for namespace in namespaces
            if namespace[limit_key] > 0
        ),
        key=lambda namespace: (
            -namespace[limit_key],
            namespace["namespace"],
        ),
    )[:limit]

    result: list[dict[str, Any]] = []
    for rank, namespace in enumerate(ranked, start=1):
        result.append(
            {
                "rank": rank,
                "namespace": namespace["namespace"],
                "request": namespace[request_key],
                "request_text": format_cpu(namespace[request_key])
                if resource == "cpu"
                else format_memory(namespace[request_key]),
                "limit": namespace[limit_key],
                "limit_text": format_cpu(namespace[limit_key])
                if resource == "cpu"
                else format_memory(namespace[limit_key]),
                "capacity_percent": percentage(
                    namespace[limit_key], capacity
                ),
            }
        )
    return result


def normalize_dashboard_data(data: dict[str, Any]) -> dict[str, Any]:
    """Fill optional dashboard fields without changing the API contract."""
    data.setdefault("version", "N/A")
    data.setdefault("namespace_count", 0)

    nodes = data.setdefault("nodes", {})
    nodes.setdefault("total", 0)
    nodes.setdefault("ready", 0)
    nodes.setdefault("not_ready", 0)
    nodes.setdefault("items", [])
    role_counts = nodes.setdefault("role_counts", {})
    for role in ("master", "infra", "worker", "other"):
        role_counts.setdefault(role, 0)

    pods = data.setdefault("pods", {})
    for key in (
        "total",
        "running",
        "pending",
        "failed",
        "succeeded",
        "unknown",
        "problem_count",
        "problem_more_count",
    ):
        pods.setdefault(key, 0)
    pods.setdefault("problem_items", [])
    pods.setdefault("problem_by_namespace", {})

    restarts = data.setdefault("restarts", {})
    restarts.setdefault("crashloop_count", 0)
    restarts.setdefault("items", [])
    restarts.setdefault("by_namespace", {})

    operators = data.setdefault("cluster_operators", {})
    operators.setdefault("available", False)
    for key in ("healthy", "degraded", "progressing", "unavailable"):
        operators.setdefault(key, 0)
    operators.setdefault("items", [])

    resources = data.setdefault("resources", {})
    cluster_resources = resources.setdefault("cluster", {})
    for key in (
        "cpu_capacity",
        "cpu_allocatable",
        "cpu_request",
        "cpu_limit",
        "memory_capacity",
        "memory_allocatable",
        "memory_request",
        "memory_limit",
        "storage_capacity",
        "storage_allocatable",
    ):
        cluster_resources.setdefault(key, 0)
    resources.setdefault("namespaces", [])
    resources.setdefault("missing_details", [])
    resources.setdefault("missing_detail_count", 0)
    resources.setdefault("missing_detail_more_count", 0)

    for node in nodes["items"]:
        node.setdefault("name", "N/A")
        node.setdefault("roles", [])
        node.setdefault("ready", False)
        for key in (
            "cpu_capacity",
            "cpu_allocatable",
            "memory_capacity",
            "memory_allocatable",
            "storage_capacity",
            "storage_allocatable",
        ):
            node.setdefault(key, 0)

    namespace_defaults = {
        "namespace": "N/A",
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
    for namespace in resources["namespaces"]:
        for key, default in namespace_defaults.items():
            namespace.setdefault(key, default)
    resources["namespaces"].sort(
        key=lambda namespace: namespace["namespace"]
    )

    return data


def prepare_dashboard_data(cluster_key: str) -> dict[str, Any]:
    started_at = time.perf_counter()
    definition = get_cluster_definition(cluster_key)
    api_client = new_cluster_client(cluster_key)

    try:
        collector = ClusterCollector(api_client)
        data = normalize_dashboard_data(collector.collect_dashboard())
    finally:
        api_client.close()

    data["selected_cluster"] = cluster_key
    data["selected_cluster_name"] = definition.name

    cluster_resources = data["resources"]["cluster"]
    cluster_resources["cpu_capacity_text"] = format_cpu(
        cluster_resources["cpu_capacity"]
    )
    cluster_resources["cpu_allocatable_text"] = format_cpu(
        cluster_resources["cpu_allocatable"]
    )
    cluster_resources["cpu_request_text"] = format_cpu(
        cluster_resources["cpu_request"]
    )
    cluster_resources["cpu_limit_text"] = format_cpu(
        cluster_resources["cpu_limit"]
    )
    cluster_resources["memory_capacity_text"] = format_memory(
        cluster_resources["memory_capacity"]
    )
    cluster_resources["memory_allocatable_text"] = format_memory(
        cluster_resources["memory_allocatable"]
    )
    cluster_resources["memory_request_text"] = format_memory(
        cluster_resources["memory_request"]
    )
    cluster_resources["memory_limit_text"] = format_memory(
        cluster_resources["memory_limit"]
    )
    cluster_resources["storage_capacity_text"] = format_memory(
        cluster_resources["storage_capacity"]
    )
    cluster_resources["storage_allocatable_text"] = format_memory(
        cluster_resources["storage_allocatable"]
    )
    cluster_resources["cpu_request_percent"] = percentage(
        cluster_resources["cpu_request"],
        cluster_resources["cpu_capacity"],
    )
    cluster_resources["cpu_limit_percent"] = percentage(
        cluster_resources["cpu_limit"],
        cluster_resources["cpu_capacity"],
    )
    cluster_resources["memory_request_percent"] = percentage(
        cluster_resources["memory_request"],
        cluster_resources["memory_capacity"],
    )
    cluster_resources["memory_limit_percent"] = percentage(
        cluster_resources["memory_limit"],
        cluster_resources["memory_capacity"],
    )
    cluster_resources["cpu_overcommit_ratio"] = round(
        cluster_resources["cpu_limit_percent"] / 100, 2
    )
    cluster_resources["memory_overcommit_ratio"] = round(
        cluster_resources["memory_limit_percent"] / 100, 2
    )
    cluster_resources["cpu_overcommitted"] = (
        cluster_resources["cpu_limit"] > cluster_resources["cpu_capacity"]
        and cluster_resources["cpu_capacity"] > 0
    )
    cluster_resources["memory_overcommitted"] = (
        cluster_resources["memory_limit"]
        > cluster_resources["memory_capacity"]
        and cluster_resources["memory_capacity"] > 0
    )

    for node in data["nodes"]["items"]:
        node["cpu_capacity_text"] = format_cpu(node["cpu_capacity"])
        node["cpu_allocatable_text"] = format_cpu(
            node["cpu_allocatable"]
        )
        node["memory_capacity_text"] = format_memory(
            node["memory_capacity"]
        )
        node["memory_allocatable_text"] = format_memory(
            node["memory_allocatable"]
        )
        node["storage_capacity_text"] = format_memory(
            node["storage_capacity"]
        )
        node["storage_allocatable_text"] = format_memory(
            node["storage_allocatable"]
        )

    for namespace in data["resources"]["namespaces"]:
        namespace["cpu_request_text"] = format_cpu(
            namespace["cpu_request"]
        )
        namespace["cpu_limit_text"] = format_cpu(
            namespace["cpu_limit"]
        )
        namespace["memory_request_text"] = format_memory(
            namespace["memory_request"]
        )
        namespace["memory_limit_text"] = format_memory(
            namespace["memory_limit"]
        )
        namespace["cpu_limit_capacity_percent"] = percentage(
            namespace["cpu_limit"],
            cluster_resources["cpu_capacity"],
        )
        namespace["memory_limit_capacity_percent"] = percentage(
            namespace["memory_limit"],
            cluster_resources["memory_capacity"],
        )
        namespace["cpu_limit_severity"] = resource_severity(
            namespace["cpu_limit"],
            cluster_resources["cpu_capacity"],
        )
        namespace["memory_limit_severity"] = resource_severity(
            namespace["memory_limit"],
            cluster_resources["memory_capacity"],
        )

    namespaces = data["resources"]["namespaces"]
    data["resources"]["namespace_options"] = [
        namespace["namespace"] for namespace in namespaces
    ]
    data["resources"]["top_limits"] = {
        "cpu": top_resource_limits(
            namespaces,
            "cpu",
            cluster_resources["cpu_capacity"],
        ),
        "memory": top_resource_limits(
            namespaces,
            "memory",
            cluster_resources["memory_capacity"],
        ),
    }
    data["health"] = health_score(data)
    data["collected_at"] = datetime.now().astimezone().strftime("%H:%M:%S")
    data["collection_duration_seconds"] = round(
        time.perf_counter() - started_at, 2
    )

    return data


def raise_http_error(exc: Exception) -> None:
    if isinstance(exc, ClusterNotFoundError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if isinstance(exc, ClusterConfigurationError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if isinstance(exc, ApiException):
        logger.exception("OpenShift API error")
        raise HTTPException(
            status_code=502,
            detail="OpenShift API çağrısı başarısız oldu.",
        ) from exc

    logger.exception("Unexpected dashboard error")
    raise HTTPException(
        status_code=500,
        detail="Beklenmeyen bir uygulama hatası oluştu.",
    ) from exc


def dashboard_error_message(exc: Exception) -> str:
    if isinstance(exc, ClusterNotFoundError):
        return str(exc)
    if isinstance(exc, ClusterConfigurationError):
        return str(exc)
    if isinstance(exc, ApiException):
        return "OpenShift API çağrısı başarısız oldu."
    return "Beklenmeyen bir uygulama hatası oluştu."


@app.get("/api/clusters")
def api_clusters() -> dict[str, Any]:
    return {
        "default": DEFAULT_CLUSTER,
        "clusters": [
            {
                "key": definition.key,
                "name": definition.name,
                "connection_type": definition.connection_type,
            }
            for definition in get_cluster_definitions().values()
        ],
    }


@app.get("/api/summary")
def api_summary(
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> dict[str, Any]:
    try:
        return prepare_dashboard_data(cluster.lower())
    except Exception as exc:
        raise_http_error(exc)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    cluster_key = cluster.lower()
    definitions = get_cluster_definitions()
    cluster_options = {
        key: {"name": definition.name}
        for key, definition in definitions.items()
    }

    try:
        data = prepare_dashboard_data(cluster_key)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "cluster_options": cluster_options,
                "selected_cluster": cluster_key,
                "selected_cluster_name": data[
                    "selected_cluster_name"
                ],
                "data": data,
                "error": None,
                "release": app.version,
            },
        )
    except Exception as exc:
        logger.exception("Dashboard rendering failed")

        selected_name = cluster_options.get(
            cluster_key,
            {"name": cluster_key.upper()},
        )["name"]

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            status_code=503,
            context={
                "cluster_options": cluster_options,
                "selected_cluster": cluster_key,
                "selected_cluster_name": selected_name,
                "data": None,
                "error": dashboard_error_message(exc),
                "release": app.version,
            },
        )
