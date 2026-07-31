from __future__ import annotations

import logging
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
    title="KKB OpenShift Control Center",
    version="0.4.0",
)

templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "application": "KOCC",
        "version": app.version,
    }


def percentage(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((value / total) * 100, 2)


def prepare_dashboard_data(cluster_key: str) -> dict[str, Any]:
    definition = get_cluster_definition(cluster_key)
    api_client = new_cluster_client(cluster_key)

    try:
        collector = ClusterCollector(api_client)
        data = collector.collect_dashboard()
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
        cluster_resources["cpu_allocatable"],
    )
    cluster_resources["cpu_limit_percent"] = percentage(
        cluster_resources["cpu_limit"],
        cluster_resources["cpu_allocatable"],
    )
    cluster_resources["memory_request_percent"] = percentage(
        cluster_resources["memory_request"],
        cluster_resources["memory_allocatable"],
    )
    cluster_resources["memory_limit_percent"] = percentage(
        cluster_resources["memory_limit"],
        cluster_resources["memory_allocatable"],
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
