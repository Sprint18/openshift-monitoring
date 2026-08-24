from __future__ import annotations

import logging
import os
import csv
import io
import math
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
from app.diagnostics import analyze_pod_diagnostics
from app.performance import (
    log_performance,
    get_perf_path,
    reset_perf_cluster,
    reset_perf_path,
    set_perf_cluster,
    set_perf_path,
)
from app.resource_parser import format_cpu, format_memory

logger = logging.getLogger("kocc")

app = FastAPI(
    title="OpenShift Clusters Monitoring Platform",
    version="0.4.0",
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
ISTANBUL_TIMEZONE = ZoneInfo("Europe/Istanbul")
def positive_env_seconds(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        logger.warning("Invalid %s; using default %s", name, default)
        return default


DASHBOARD_CACHE_TTL_SECONDS = positive_env_seconds(
    "KOCC_SNAPSHOT_TTL_SECONDS", 60
)
DIAGNOSTIC_CACHE_TTL_SECONDS = positive_env_seconds(
    "KOCC_DIAGNOSTICS_CACHE_TTL_SECONDS", 60
)
_dashboard_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_diagnostic_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_dashboard_cache_lock = threading.Lock()
_cluster_cache_locks: dict[str, threading.Lock] = {}
logger.info(
    "OpenShift Clusters Monitoring Platform started version=%s pid=%s startup_timestamp=%s",
    app.version,
    os.getpid(),
    datetime.now(timezone.utc).isoformat(),
)


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next: Any) -> Any:
    started = time.perf_counter()
    status = 500
    cluster_key = request.query_params.get("cluster", DEFAULT_CLUSTER).lower()
    perf_token = set_perf_cluster(cluster_key)
    path_token = set_perf_path(request.url.path)
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "request path=%s status=%s duration_ms=%s",
            request.url.path,
            status,
            duration_ms,
        )
        log_performance(
            "total.request",
            started,
            item_count=1,
            extra={"path": request.url.path, "status": status},
        )
        reset_perf_cluster(perf_token)
        reset_perf_path(path_token)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "application": "OpenShift Clusters Monitoring Platform",
        "version": app.version,
    }


@app.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready", "version": app.version}


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


def format_istanbul_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ISTANBUL_TIMEZONE).strftime("%H:%M:%S")


def collection_time_severity(seconds: float) -> str:
    if seconds >= 10:
        return "slow"
    if seconds >= 5:
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
    rank_by: str = "limit",
) -> list[dict[str, Any]]:
    limit_key = f"{resource}_limit"
    request_key = f"{resource}_request"
    rank_key = request_key if rank_by == "request" else limit_key
    ranked = sorted(
        (
            namespace
            for namespace in namespaces
            if namespace[rank_key] > 0
        ),
        key=lambda namespace: (
            -namespace[rank_key],
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
                    namespace[rank_key], capacity
                ),
            }
        )
    return result


EXECUTIVE_TOP_LIMIT = 10
NAMESPACE_SLICE_THRESHOLD_PERCENT = 1.0
MAX_NAMESPACE_SLICES = 10
CONCENTRATION_WARNING_PERCENT = 20.0
CONCENTRATION_CRITICAL_PERCENT = 30.0
PLATFORM_NAMESPACE_PREFIXES = ("openshift-", "kube-")
PLATFORM_NAMESPACE_NAMES = {
    "default", "istio-system", "openshift-monitoring", "dynatrace",
}
OVERCOMMIT_WARNING_PERCENT = 100
OVERCOMMIT_CRITICAL_PERCENT = 150
OVERCOMMIT_HIGH_PERCENT = 150
OVERCOMMIT_CRITICAL_EXECUTIVE_PERCENT = 250


def is_platform_namespace(namespace: str) -> bool:
    return (
        namespace in PLATFORM_NAMESPACE_NAMES
        or namespace.startswith(PLATFORM_NAMESPACE_PREFIXES)
    )


def risk_level(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "healthy"


def capacity_risk_level(value: float) -> str:
    if value >= OVERCOMMIT_CRITICAL_EXECUTIVE_PERCENT:
        return "critical"
    if value >= OVERCOMMIT_HIGH_PERCENT:
        return "high"
    if value >= OVERCOMMIT_WARNING_PERCENT:
        return "warning"
    return "healthy"


def egressip_overview(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Normalize optional cached EgressIP data without performing an API call."""
    normalized = []
    for item in items or []:
        node = item.get("assigned_node") or ""
        normalized.append({
            "name": item.get("name", "unknown"),
            "namespace": item.get("namespace", "N/A"),
            "assigned_node": node or "Unassigned",
            "status": "Healthy" if node else "Unassigned",
        })
    unassigned = sum(item["status"] == "Unassigned" for item in normalized)
    return {
        "available": items is not None,
        "items": normalized,
        "total": len(normalized),
        "unassigned": unassigned,
        "status": "Attention Required" if unassigned else "Healthy",
    }


def allocation_distribution(
    platform: int, applications: int, capacity: int, resource: str,
) -> dict[str, Any]:
    used = platform + applications
    unused = max(0, capacity - used)
    chart_total = max(capacity, used, 1)
    formatter = format_cpu if resource == "cpu" else format_memory
    return {
        "platform": platform,
        "applications": applications,
        "unused": unused,
        "platform_text": formatter(platform),
        "applications_text": formatter(applications),
        "unused_text": formatter(unused),
        "platform_percent": round(platform / chart_total * 100, 2),
        "applications_percent": round(applications / chart_total * 100, 2),
        "unused_percent": round(unused / chart_total * 100, 2),
        "request_capacity_percent": percentage(used, capacity),
    }


def namespace_request_distribution(
    namespaces: list[dict[str, Any]], resource: str, capacity: int,
) -> dict[str, Any]:
    """Group a capacity-based namespace request distribution for one chart."""
    key = f"{resource}_request"
    formatter = format_cpu if resource == "cpu" else format_memory
    ranked = sorted(
        namespaces,
        key=lambda item: (-int(item.get(key, 0) or 0), item["namespace"]),
    )
    individually_visible = [
        item for item in ranked
        if capacity > 0
        and percentage(int(item.get(key, 0) or 0), capacity)
        >= NAMESPACE_SLICE_THRESHOLD_PERCENT
    ]
    visible = individually_visible[:MAX_NAMESPACE_SLICES]
    visible_names = {item["namespace"] for item in visible}
    grouped = [item for item in ranked if item["namespace"] not in visible_names]
    total_requests = sum(int(item.get(key, 0) or 0) for item in ranked)
    unused = max(capacity - total_requests, 0)
    visual_total = max(capacity, total_requests, 1)
    slices: list[dict[str, Any]] = []
    for item in visible:
        value = int(item.get(key, 0) or 0)
        slices.append({
            "label": item["namespace"], "value": value,
            "value_text": formatter(value),
            "capacity_percent": percentage(value, capacity),
            "visual_percent": round(value / visual_total * 100, 4),
            "grouped_count": 0, "kind": "namespace",
        })
    grouped_value = sum(int(item.get(key, 0) or 0) for item in grouped)
    if grouped:
        slices.append({
            "label": "Diğerleri", "value": grouped_value,
            "value_text": formatter(grouped_value),
            "capacity_percent": percentage(grouped_value, capacity),
            "visual_percent": round(grouped_value / visual_total * 100, 4),
            "grouped_count": len(grouped), "kind": "others",
        })
    if unused > 0:
        slices.append({
            "label": "Unused Capacity", "value": unused,
            "value_text": formatter(unused),
            "capacity_percent": percentage(unused, capacity),
            "visual_percent": round(unused / visual_total * 100, 4),
            "grouped_count": 0, "kind": "unused",
        })
    cursor = 0.0
    for item in slices:
        item["start_percent"] = round(cursor, 4)
        cursor += item["visual_percent"]
        item["end_percent"] = round(cursor, 4)
        item["svg_path"] = pie_slice_path(
            item["start_percent"], item["end_percent"]
        )
    top = next((item for item in slices if item["kind"] == "namespace"), None)
    top_share = top["capacity_percent"] if top else 0.0
    concentration = risk_level(
        top_share,
        CONCENTRATION_WARNING_PERCENT,
        CONCENTRATION_CRITICAL_PERCENT,
    )
    return {
        "resource": resource,
        "capacity": capacity,
        "capacity_text": formatter(capacity),
        "total_requests": total_requests,
        "total_requests_text": formatter(total_requests),
        "request_ratio": percentage(total_requests, capacity),
        "unused": unused,
        "unused_text": formatter(unused),
        "over_capacity": total_requests > capacity,
        "slices": slices,
        "total_namespaces": len(namespaces),
        "visible_namespace_count": len(visible),
        "threshold_namespace_count": len(individually_visible),
        "grouped_namespace_count": len(grouped),
        "top_namespace": top["label"] if top else "N/A",
        "top_value_text": top["value_text"] if top else formatter(0),
        "top_share": top_share,
        "concentration": concentration,
        "slice_threshold_percent": NAMESPACE_SLICE_THRESHOLD_PERCENT,
        "max_namespace_slices": MAX_NAMESPACE_SLICES,
    }


def pie_slice_path(start_percent: float, end_percent: float) -> str:
    """Return a deterministic SVG wedge path for a percentage interval."""
    center = 100.0
    radius = 94.0
    span = max(0.0, end_percent - start_percent)
    if span >= 99.999:
        return (
            "M 100 6 A 94 94 0 1 1 99.999 6 "
            "A 94 94 0 1 1 100 6 Z"
        )
    start_angle = math.radians(start_percent * 3.6 - 90)
    end_angle = math.radians(end_percent * 3.6 - 90)
    start_x = center + radius * math.cos(start_angle)
    start_y = center + radius * math.sin(start_angle)
    end_x = center + radius * math.cos(end_angle)
    end_y = center + radius * math.sin(end_angle)
    large_arc = 1 if span > 50 else 0
    return (
        f"M 100 100 L {start_x:.3f} {start_y:.3f} "
        f"A 94 94 0 {large_arc} 1 {end_x:.3f} {end_y:.3f} Z"
    )


def executive_dashboard(data: dict[str, Any]) -> dict[str, Any]:
    """Build executive-only data from the existing cached snapshot."""
    namespaces = data["resources"]["namespaces"]
    cluster = data["resources"]["cluster"]
    platform = [item for item in namespaces if is_platform_namespace(item["namespace"])]
    applications = [item for item in namespaces if not is_platform_namespace(item["namespace"])]

    def total(items: list[dict[str, Any]], key: str) -> int:
        return sum(int(item.get(key, 0) or 0) for item in items)

    cpu = allocation_distribution(
        total(platform, "cpu_request"), total(applications, "cpu_request"),
        cluster["cpu_capacity"], "cpu",
    )
    memory = allocation_distribution(
        total(platform, "memory_request"), total(applications, "memory_request"),
        cluster["memory_capacity"], "memory",
    )
    request_distributions = {
        "cpu": namespace_request_distribution(
            namespaces, "cpu", cluster["cpu_capacity"]
        ),
        "memory": namespace_request_distribution(
            namespaces, "memory", cluster["memory_capacity"]
        ),
    }

    def ranking(resource: str, mode: str, source: list[dict[str, Any]]) -> list[dict[str, Any]]:
        key = f"{resource}_{mode}"
        capacity = cluster[f"{resource}_capacity"]
        formatter = format_cpu if resource == "cpu" else format_memory
        ranked = sorted(
            source,
            key=lambda item: (-int(item.get(key, 0) or 0), item["namespace"]),
        )[:EXECUTIVE_TOP_LIMIT]
        return [{
            "namespace": item["namespace"],
            "value": item[key],
            "value_text": formatter(item[key]),
            "capacity_percent": percentage(item[key], capacity),
        } for item in ranked if item[key] > 0]

    restart_by_namespace = data["restarts"]["restart_by_namespace"]
    crashloop_by_namespace = data["restarts"]["crashloop_by_namespace"]
    missing_by_namespace = {
        item["namespace"]: sum(int(item.get(key, 0) or 0) for key in (
            "missing_cpu_request", "missing_cpu_limit",
            "missing_memory_request", "missing_memory_limit",
        ))
        for item in namespaces
    }

    def maximum(mapping: dict[str, int]) -> tuple[str, int]:
        return max(mapping.items(), key=lambda item: (item[1], item[0])) if mapping else ("N/A", 0)

    cpu_top = maximum({item["namespace"]: item["cpu_request"] for item in namespaces})
    memory_top = maximum({item["namespace"]: item["memory_request"] for item in namespaces})
    restart_top = maximum(restart_by_namespace)
    crashloop_top = maximum(crashloop_by_namespace)
    missing_top = maximum(missing_by_namespace)
    cpu_limit_percent = percentage(cluster["cpu_limit"], cluster["cpu_capacity"])
    memory_limit_percent = percentage(cluster["memory_limit"], cluster["memory_capacity"])
    capacity_pressure = max(cpu["request_capacity_percent"], memory["request_capacity_percent"])
    health = data["health"]
    operators = data["cluster_operators"]
    nodes = data.get("nodes", {"total": 0, "ready": 0, "not_ready": 0})
    missing_count = data["resources"]["missing_resources"]["application"]["count"]
    pod_reasons = data["pods"].get("reason_counts", {})
    kpis = [
        {"label": "Overall Health Score", "value": f'{health["score"]}/100', "risk": health["status"].lower(), "icon": "♥"},
        {"label": "Cluster Request Allocation", "value": f"%{capacity_pressure}", "risk": risk_level(capacity_pressure, 75, 90), "icon": "◫"},
        {"label": "CPU Overcommit", "value": f"%{cpu_limit_percent}", "risk": risk_level(cpu_limit_percent, OVERCOMMIT_WARNING_PERCENT, OVERCOMMIT_CRITICAL_PERCENT), "icon": "⚙"},
        {"label": "Memory Overcommit", "value": f"%{memory_limit_percent}", "risk": risk_level(memory_limit_percent, OVERCOMMIT_WARNING_PERCENT, OVERCOMMIT_CRITICAL_PERCENT), "icon": "▦"},
        {"label": "Problem Pods", "value": data["pods"]["problem_count"], "risk": risk_level(data["pods"]["problem_count"], 1, 10), "icon": "!", "detail": f'CrashLoopBackOff {pod_reasons.get("CrashLoopBackOff", 0)} · ImagePullBackOff {pod_reasons.get("ImagePullBackOff", 0)} · Pending {data["pods"].get("pending", 0)}'},
        {"label": "Missing Resources", "value": missing_count, "risk": risk_level(missing_count, 1, 100), "icon": "∅"},
        {"label": "Cluster Operators Healthy", "value": operators["healthy"] if operators["available"] else "N/A", "risk": "healthy" if operators["available"] and not operators["degraded"] and not operators["unavailable"] else "critical", "icon": "✓"},
    ]
    insights: list[str] = []
    actions: list[dict[str, str]] = []
    if cpu_limit_percent >= OVERCOMMIT_WARNING_PERCENT:
        insights.append(f"CPU limitleri kapasitenin %{cpu_limit_percent} seviyesinde.")
        actions.append({"text": "CPU limit tanımlarını gözden geçirin.", "href": "/resources"})
    if memory_limit_percent >= OVERCOMMIT_WARNING_PERCENT:
        insights.append(f"Memory limitleri kapasitenin %{memory_limit_percent} seviyesinde.")
        actions.append({"text": "Memory limit tanımlarını gözden geçirin.", "href": "/resources"})
    if data["restarts"]["crashloop_count"]:
        insights.append(f'{data["restarts"]["crashloop_count"]} CrashLoopBackOff pod bulunuyor.')
        actions.append({"text": "CrashLoopBackOff podlarını Diagnostics ekranından inceleyin.", "href": "/diagnostics"})
    if missing_count:
        insights.append(f"{missing_count} eksik request/limit tanımı bulunuyor.")
        actions.append({"text": "Eksik resource tanımlarını Resources ekranından düzeltin.", "href": "/resources"})
    for resource, label in (("cpu", "CPU"), ("memory", "Memory")):
        distribution_item = request_distributions[resource]
        if distribution_item["top_namespace"] != "N/A":
            concentration_prefix = (
                "Critical concentration: "
                if distribution_item["concentration"] == "critical"
                else "High concentration: "
                if distribution_item["concentration"] == "warning"
                else ""
            )
            insights.append(
                f'{concentration_prefix}{distribution_item["top_namespace"]} tek başına cluster {label} '
                f'capacity değerinin %{distribution_item["top_share"]} seviyesinde request tanımlıyor.'
            )
        category = cpu if resource == "cpu" else memory
        insights.append(
            f'Platform namespace’leri toplam {label} capacity değerinin '
            f'%{category["platform_percent"]} seviyesinde request tanımlıyor; '
            f'application namespace’leri %{category["applications_percent"]} seviyesinde.'
        )
    if not insights:
        insights.append("Belirlenen executive risk eşiklerinde kritik bulgu yok.")

    return {
        "kpis": kpis, "cpu": cpu, "memory": memory,
        "request_distributions": request_distributions,
        "rankings": {
            "cpu_request": ranking("cpu", "request", namespaces),
            "memory_request": ranking("memory", "request", namespaces),
            "cpu_limit": ranking("cpu", "limit", namespaces),
            "memory_limit": ranking("memory", "limit", namespaces),
            "all": {
                "cpu_request": ranking("cpu", "request", namespaces),
                "memory_request": ranking("memory", "request", namespaces),
                "cpu_limit": ranking("cpu", "limit", namespaces),
                "memory_limit": ranking("memory", "limit", namespaces),
            },
            "application": {
                "cpu_request": ranking("cpu", "request", applications),
                "memory_request": ranking("memory", "request", applications),
                "cpu_limit": ranking("cpu", "limit", applications),
                "memory_limit": ranking("memory", "limit", applications),
            },
        },
        "gauges": {
            "cpu": {"percent": cpu_limit_percent, "risk": capacity_risk_level(cpu_limit_percent), "explanation": f"Tanımlı CPU limitleri fiziksel kapasitenin {cpu_limit_percent / 100:.2f} katı."},
            "memory": {"percent": memory_limit_percent, "risk": capacity_risk_level(memory_limit_percent), "explanation": f"Tanımlı Memory limitleri fiziksel kapasitenin {memory_limit_percent / 100:.2f} katı."},
        },
        "platform_overview": {
            "nodes": {
                "total": nodes.get("total", 0), "ready": nodes.get("ready", 0),
                "not_ready": nodes.get("not_ready", 0),
                "cpu_capacity": format_cpu(cluster["cpu_capacity"]),
                "memory_capacity": format_memory(cluster["memory_capacity"]),
            },
            "operators": operators,
            "routes": {"available": False, "message": "Base snapshot'ta toplanmıyor"},
            "storage": {"available": False, "message": "Base snapshot'ta toplanmıyor"},
            "conjur": next((item for item in data.get("platform_controls", []) if item["name"] == "Conjur Pods"), {"status": "Unavailable", "detail": "Data unavailable"}),
        },
        "critical_controls": data.get("platform_controls", []),
        "egressips": egressip_overview(data.get("egressips") if "egressips" in data else None),
        "hotspots": [
            {"label": "En yüksek CPU request", "namespace": cpu_top[0], "value": format_cpu(cpu_top[1]), "href": "/resources"},
            {"label": "En yüksek Memory request", "namespace": memory_top[0], "value": format_memory(memory_top[1]), "href": "/resources"},
            {"label": "En çok restart", "namespace": restart_top[0], "value": restart_top[1], "href": "/diagnostics"},
            {"label": "En çok CrashLoopBackOff", "namespace": crashloop_top[0], "value": crashloop_top[1], "href": "/diagnostics"},
            {"label": "En çok eksik resource", "namespace": missing_top[0], "value": missing_top[1], "href": "/resources"},
        ],
        "insights": insights, "actions": actions,
        "trend": {
            "cpu": cpu["request_capacity_percent"],
            "memory": memory["request_capacity_percent"],
            "health": health["score"],
        },
    }


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
    pods.setdefault("diagnostic_items", [])

    restarts = data.setdefault("restarts", {})
    restarts.setdefault("crashloop_count", 0)
    restarts.setdefault("items", [])
    restarts.setdefault("by_namespace", {})
    restarts.setdefault("restart_by_namespace", {})
    restarts.setdefault("crashloop_by_namespace", {})

    search = data.setdefault("search", {})
    search.setdefault("pods", [])

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
    missing_resources = resources.setdefault("missing_resources", {})
    for scope in ("application", "all"):
        summary = missing_resources.setdefault(scope, {})
        for key in ("count", "namespace_count", "container_count", "more_count"):
            summary.setdefault(key, 0)
        summary.setdefault("items", [])
        summary.setdefault("records", [])

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
    context_started = time.perf_counter()

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
    data["resources"]["top_requests"] = {
        "cpu": top_resource_limits(
            namespaces,
            "cpu",
            cluster_resources["cpu_capacity"],
            rank_by="request",
        ),
        "memory": top_resource_limits(
            namespaces,
            "memory",
            cluster_resources["memory_capacity"],
            rank_by="request",
        ),
    }
    health_started = time.perf_counter()
    data["health"] = health_score(data)
    log_performance("process.health_score", health_started, item_count=1)
    executive_started = time.perf_counter()
    data["executive"] = executive_dashboard(data)
    log_performance("process.executive_dashboard", executive_started, item_count=1)
    data["collected_at"] = format_istanbul_time(datetime.now(timezone.utc))
    data["collection_duration_seconds"] = round(
        time.perf_counter() - started_at, 2
    )
    data["collection_duration_severity"] = collection_time_severity(
        data["collection_duration_seconds"]
    )
    log_performance("template.context_build", context_started, item_count=1)

    return data


def clear_dashboard_cache() -> None:
    with _dashboard_cache_lock:
        _dashboard_cache.clear()
        _cluster_cache_locks.clear()
        _diagnostic_cache.clear()


def _cached_dashboard_data(
    cluster_key: str, force_refresh: bool = False
) -> dict[str, Any]:
    """Return an isolated cluster snapshot with a short thread-safe TTL."""
    now = time.monotonic()
    cache_started = time.perf_counter()
    with _dashboard_cache_lock:
        cached = _dashboard_cache.get(cluster_key)
        if not force_refresh and cached and now - cached[0] < DASHBOARD_CACHE_TTL_SECONDS:
            age_ms = round((now - cached[0]) * 1000)
            logger.info(
                "snapshot cluster=%s path=%s cache=HIT age_ms=%s ttl_ms=%s",
                cluster_key, get_perf_path(), age_ms,
                DASHBOARD_CACHE_TTL_SECONDS * 1000,
            )
            data = deepcopy(cached[1])
            data["cache"] = {
                "hit": True,
                "age_seconds": round(now - cached[0], 2),
            }
            log_performance(
                "cache.snapshot", cache_started, cache_hit=True,
                extra={"snapshot_age_ms": age_ms},
            )
            return data
        cluster_lock = _cluster_cache_locks.setdefault(
            cluster_key, threading.Lock()
        )

    with cluster_lock:
        now = time.monotonic()
        with _dashboard_cache_lock:
            cached = _dashboard_cache.get(cluster_key)
            if not force_refresh and cached and now - cached[0] < DASHBOARD_CACHE_TTL_SECONDS:
                age_ms = round((now - cached[0]) * 1000)
                logger.info(
                    "snapshot cluster=%s path=%s cache=HIT age_ms=%s ttl_ms=%s",
                    cluster_key, get_perf_path(), age_ms,
                    DASHBOARD_CACHE_TTL_SECONDS * 1000,
                )
                data = deepcopy(cached[1])
                data["cache"] = {
                    "hit": True,
                    "age_seconds": round(now - cached[0], 2),
                }
                log_performance(
                    "cache.snapshot", cache_started, cache_hit=True,
                    extra={"snapshot_age_ms": age_ms},
                )
                return data
        logger.info(
            "snapshot cluster=%s path=%s cache=%s age_ms=%s ttl_ms=%s",
            cluster_key, get_perf_path(),
            "BYPASS" if force_refresh else "MISS",
            round((now - cached[0]) * 1000) if cached else 0,
            DASHBOARD_CACHE_TTL_SECONDS * 1000,
        )
        log_performance("cache.snapshot", cache_started, cache_hit=False)
        try:
            data = prepare_dashboard_data(cluster_key)
        except Exception:
            if cached:
                logger.warning(
                    "Serving stale successful snapshot for cluster %s",
                    cluster_key,
                )
                data = deepcopy(cached[1])
                data["cache"] = {
                    "hit": True,
                    "stale": True,
                    "age_seconds": round(now - cached[0], 2),
                }
                log_performance(
                    "cache.stale_fallback", cache_started, cache_hit=True,
                    extra={"snapshot_age_ms": round((now - cached[0]) * 1000)},
                )
                return data
            raise
        with _dashboard_cache_lock:
            _dashboard_cache[cluster_key] = (time.monotonic(), deepcopy(data))
    data["cache"] = {"hit": False, "age_seconds": 0.0}
    return data


def cached_dashboard_data(
    cluster_key: str, force_refresh: bool = False
) -> dict[str, Any]:
    token = set_perf_cluster(cluster_key)
    try:
        return _cached_dashboard_data(cluster_key, force_refresh)
    finally:
        reset_perf_cluster(token)


MISSING_SORT_FIELDS = {
    "namespace", "pod", "container", "missing_count",
    "cpu_request", "cpu_limit", "memory_request", "memory_limit",
}


def missing_resources_page(
    records: list[dict[str, Any]],
    *,
    q: str = "",
    include_openshift: bool = False,
    sort: str = "namespace",
    direction: str = "asc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Apply the canonical missing-resource query pipeline."""
    sort_key = sort if sort in MISSING_SORT_FIELDS else "namespace"
    descending = direction.lower() == "desc"
    query = q.strip().casefold()
    filtered = [
        item for item in records
        if (include_openshift or not str(item.get("namespace", "")).startswith("openshift-"))
        and (
            not query
            or any(
                query in str(item.get(field, "")).casefold()
                for field in ("namespace", "pod", "container")
            )
        )
    ]
    numeric = sort_key == "missing_count"
    filtered.sort(
        key=lambda item: (
            int(item.get(sort_key, 0) or 0)
            if numeric
            else str(item.get(sort_key, "")).casefold()
        ),
        reverse=descending,
    )
    total = len(filtered)
    namespace_count = len({item.get("namespace", "") for item in filtered})
    pages = max(1, (total + page_size - 1) // page_size)
    safe_page = min(max(page, 1), pages)
    start = (safe_page - 1) * page_size
    return {
        "records": filtered[start:start + page_size],
        "total": total,
        "page": safe_page,
        "page_size": page_size,
        "pages": pages,
        "namespace_count": namespace_count,
    }


def query_cached_missing_resources(
    cluster_key: str, **options: Any
) -> dict[str, Any]:
    snapshot = cached_dashboard_data(cluster_key)
    records = snapshot["resources"]["missing_resources"]["all"]["records"]
    return missing_resources_page(records, **options)


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
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return cached_dashboard_data(cluster.lower(), force_refresh=refresh)
    except Exception as exc:
        raise_http_error(exc)


@app.get("/api/resources/missing")
def api_missing_resources(
    cluster: str = Query(default=DEFAULT_CLUSTER),
    q: str = Query(default=""),
    include_openshift: bool = Query(default=False),
    sort: str = Query(default="namespace"),
    direction: str = Query(default="asc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return query_cached_missing_resources(
            cluster.lower(), q=q, include_openshift=include_openshift,
            sort=sort, direction=direction, page=page,
            page_size=page_size,
        )
    except Exception as exc:
        raise_http_error(exc)


@app.get("/api/resources/missing.csv")
def api_missing_resources_csv(
    cluster: str = Query(default=DEFAULT_CLUSTER),
    q: str = Query(default=""),
    include_openshift: bool = Query(default=False),
    sort: str = Query(default="namespace"),
    direction: str = Query(default="asc"),
) -> StreamingResponse:
    try:
        snapshot = cached_dashboard_data(cluster.lower())
        records = snapshot["resources"]["missing_resources"]["all"]["records"]
        result = missing_resources_page(
            records, q=q, include_openshift=include_openshift,
            sort=sort, direction=direction, page=1,
            page_size=max(1, len(records)),
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Namespace", "Pod", "Container", "CPU Request", "CPU Limit",
            "Memory Request", "Memory Limit", "Missing Count",
        ])
        for item in result["records"]:
            writer.writerow([
                item["namespace"], item["pod"], item["container"],
                "Defined" if item["cpu_request"] else "Missing",
                "Defined" if item["cpu_limit"] else "Missing",
                "Defined" if item["memory_request"] else "Missing",
                "Defined" if item["memory_limit"] else "Missing",
                item["missing_count"],
            ])
        return StreamingResponse(
            iter([output.getvalue()]), media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{cluster.lower()}-missing-resources.csv"'
                )
            },
        )
    except Exception as exc:
        raise_http_error(exc)


def optional_cluster_data(
    cluster_key: str,
    loader: str,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Load an optional widget without failing the main dashboard."""
    get_cluster_definition(cluster_key)
    api_client = new_cluster_client(cluster_key)
    try:
        collector = ClusterCollector(api_client)
        try:
            if loader == "workloads":
                items = collector.get_workload_summary(namespace)
                for item in items:
                    item["cpu_request_text"] = format_cpu(item["cpu_request"])
                    item["cpu_limit_text"] = format_cpu(item["cpu_limit"])
                    item["memory_request_text"] = format_memory(
                        item["memory_request"]
                    )
                    item["memory_limit_text"] = format_memory(
                        item["memory_limit"]
                    )
                return {"available": True, "items": items}
            if loader == "pvc":
                result = collector.get_pvc_summary()
                result["requested_capacity_text"] = format_memory(
                    result["requested_capacity"]
                )
                for item in result["items"]:
                    item["requested_capacity_text"] = format_memory(
                        item["requested_capacity"]
                    )
                return {"available": True, **result}
            return {
                "available": True,
                "items": collector.get_route_summary(),
            }
        except Exception as exc:
            logger.warning(
                "%s data unavailable for cluster %s: %s",
                loader,
                cluster_key,
                exc,
            )
            return {"available": False, "items": []}
    finally:
        api_client.close()


@app.get("/api/workloads")
def api_workloads(
    cluster: str = Query(default=DEFAULT_CLUSTER),
    namespace: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return optional_cluster_data(cluster.lower(), "workloads", namespace)
    except Exception as exc:
        raise_http_error(exc)


@app.get("/api/pvcs")
def api_pvcs(
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> dict[str, Any]:
    try:
        return optional_cluster_data(cluster.lower(), "pvc")
    except Exception as exc:
        raise_http_error(exc)


@app.get("/api/routes")
def api_routes(
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> dict[str, Any]:
    try:
        return optional_cluster_data(cluster.lower(), "routes")
    except Exception as exc:
        raise_http_error(exc)


@app.get("/api/diagnostics/pods")
def api_diagnostic_pods(
    cluster: str = Query(default=DEFAULT_CLUSTER),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    cluster_key = cluster.lower()
    perf_token = set_perf_cluster(cluster_key)
    try:
        get_cluster_definition(cluster_key)
        now = time.monotonic()
        with _dashboard_cache_lock:
            dashboard_cached = _dashboard_cache.get(cluster_key)
            if (
                not refresh
                and dashboard_cached
                and now - dashboard_cached[0] < DASHBOARD_CACHE_TTL_SECONDS
                and "diagnostic_items" in dashboard_cached[1].get("pods", {})
            ):
                items = deepcopy(
                    dashboard_cached[1]["pods"]["diagnostic_items"]
                )
                age_ms = round((now - dashboard_cached[0]) * 1000)
                log_performance(
                    "cache.diagnostics_dashboard_snapshot",
                    time.perf_counter(), item_count=len(items),
                    cache_hit=True, extra={"snapshot_age_ms": age_ms},
                )
                return {"available": True, "items": items}
            cached = _diagnostic_cache.get(cluster_key)
            if not refresh and cached and now - cached[0] < DIAGNOSTIC_CACHE_TTL_SECONDS:
                age_ms = round((now - cached[0]) * 1000)
                log_performance(
                    "cache.diagnostics", time.perf_counter(),
                    item_count=len(cached[1]), cache_hit=True,
                    extra={"snapshot_age_ms": age_ms},
                )
                return {"available": True, "items": deepcopy(cached[1])}
        log_performance(
            "cache.diagnostics", time.perf_counter(), cache_hit=False
        )
        api_client = new_cluster_client(cluster_key)
        try:
            items = ClusterCollector(api_client).get_problem_pods()
            with _dashboard_cache_lock:
                _diagnostic_cache[cluster_key] = (
                    time.monotonic(), deepcopy(items)
                )
            return {"available": True, "items": items}
        finally:
            api_client.close()
    except Exception as exc:
        raise_http_error(exc)
    finally:
        reset_perf_cluster(perf_token)


@app.get("/api/diagnostics/{namespace}/{pod_name}")
def api_pod_diagnostic(
    namespace: str,
    pod_name: str,
    cluster: str = Query(default=DEFAULT_CLUSTER),
    container: str | None = Query(default=None),
    tail: int = Query(default=200),
) -> dict[str, Any]:
    if tail not in {50, 100, 200, 500}:
        raise HTTPException(
            status_code=400,
            detail="tail must be one of 50, 100, 200 or 500",
        )
    cluster_key = cluster.lower()
    perf_token = set_perf_cluster(cluster_key)
    try:
        get_cluster_definition(cluster_key)
        api_client = new_cluster_client(cluster_key)
        try:
            result = ClusterCollector(api_client).get_pod_diagnostic(
                namespace, pod_name, container, tail
            )
            analysis_started = time.perf_counter()
            result["analysis"] = analyze_pod_diagnostics(
                result["pod"], result["events"], result["logs"]
            )
            log_performance(
                "diagnostics.analysis", analysis_started, item_count=1
            )
            return result
        finally:
            api_client.close()
    except Exception as exc:
        raise_http_error(exc)
    finally:
        reset_perf_cluster(perf_token)


def diagnostics_template_context(
    cluster: str,
    namespace: str | None = None,
    pod_name: str | None = None,
) -> dict[str, Any]:
    cluster_key = cluster.lower()
    definitions = get_cluster_definitions()
    if cluster_key not in definitions:
        raise HTTPException(status_code=400, detail="Unknown cluster")
    return {
        "cluster_options": {
            key: {"name": definition.name}
            for key, definition in definitions.items()
        },
        "selected_cluster": cluster_key,
        "selected_cluster_name": definitions[cluster_key].name,
        "release": app.version,
        "namespace": namespace,
        "pod_name": pod_name,
    }


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="diagnostics.html",
        context=diagnostics_template_context(cluster),
    )


@app.get("/diagnostics/{namespace}/{pod_name}", response_class=HTMLResponse)
def pod_diagnostic_page(
    request: Request,
    namespace: str,
    pod_name: str,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="diagnostic_detail.html",
        context=diagnostics_template_context(cluster, namespace, pod_name),
    )


def render_dashboard_page(
    request: Request,
    cluster: str,
    page: str,
    force_refresh: bool = False,
    missing_options: dict[str, Any] | None = None,
) -> HTMLResponse:
    cluster_key = cluster.lower()
    definitions = get_cluster_definitions()
    cluster_options = {
        key: {"name": definition.name}
        for key, definition in definitions.items()
    }
    missing_fallback: dict[str, Any] | None = None

    try:
        if page in {"workloads", "storage", "routes"}:
            data = {
                "selected_cluster_name": definitions[cluster_key].name,
                "collected_at": format_istanbul_time(datetime.now(timezone.utc)),
                "collection_duration_seconds": 0,
                "collection_duration_severity": "normal",
                "cache": {"hit": False, "age_seconds": 0},
                "resources": {
                    "namespaces": [], "namespace_options": [],
                    "missing_resources": {"application": {"records": []}, "all": {"records": []}},
                },
                "pods": {"problem_by_namespace": {}},
                "restarts": {"by_namespace": {}, "items": []},
                "search": {"pods": []},
            }
        else:
            data = cached_dashboard_data(
                cluster_key, force_refresh=force_refresh
            )
        if page == "resources":
            missing = data["resources"]["missing_resources"]
            options = missing_options or {}
            fallback = missing_resources_page(
                missing["all"]["records"], **options
            )
            missing_fallback = fallback
            missing["application"]["records"] = fallback["records"]
            missing["application"]["count"] = fallback["total"]
            missing["application"]["container_count"] = fallback["total"]
            missing["application"]["namespace_count"] = fallback[
                "namespace_count"
            ]
            logger.info(
                "resources_page_payload missing_application=%s missing_all=%s",
                len(missing["application"]["records"]),
                len(missing["all"]["records"]),
            )
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
                "page": page,
                "missing_options": missing_options or {},
                "missing_fallback": missing_fallback,
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
                "page": page,
                "missing_options": missing_options or {},
                "missing_fallback": missing_fallback,
            },
        )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
    refresh: bool = Query(default=False),
) -> HTMLResponse:
    return render_dashboard_page(request, cluster, "overview", refresh)


@app.get("/resources", response_class=HTMLResponse)
def resources_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
    refresh: bool = Query(default=False),
    missing_q: str = Query(default=""),
    include_openshift: bool = Query(default=False),
    missing_sort: str = Query(default="namespace"),
    missing_direction: str = Query(default="asc"),
    missing_page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    return render_dashboard_page(
        request, cluster, "resources", refresh,
        {
            "q": missing_q,
            "include_openshift": include_openshift,
            "sort": missing_sort,
            "direction": missing_direction,
            "page": missing_page,
            "page_size": 50,
        },
    )


@app.get("/workloads", response_class=HTMLResponse)
def workloads_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return render_dashboard_page(request, cluster, "workloads")


@app.get("/storage", response_class=HTMLResponse)
def storage_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return render_dashboard_page(request, cluster, "storage")


@app.get("/routes", response_class=HTMLResponse)
def routes_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return render_dashboard_page(request, cluster, "routes")


@app.get("/health-overview", response_class=HTMLResponse)
def health_overview_page(
    request: Request,
    cluster: str = Query(default=DEFAULT_CLUSTER),
) -> HTMLResponse:
    return render_dashboard_page(request, cluster, "health")
