from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.clusters import UnknownClusterError, cluster_registry, selected_cluster
from app.config import Settings, load_settings
from app.llm_client import LLMClient, LLMUnavailable
from app.mcp_client import MCPClient, MCPUnavailable


logger = logging.getLogger("kocc_ai")


class ChatRequest(BaseModel):
    cluster: str
    message: str = Field(min_length=1, max_length=8000)


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings or load_settings()
    application = FastAPI(title="KOCC AI Backend", version="0.1.0")
    application.state.settings = configuration
    application.state.clusters = cluster_registry(configuration)
    application.state.llm_client = LLMClient(configuration)

    @application.middleware("http")
    async def request_logging(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request request_id=%s path=%s status=%s duration_ms=%s",
            request_id, request.url.path, response.status_code,
            round((time.perf_counter() - started) * 1000),
        )
        return response

    def mcp_for(cluster_id: str) -> tuple[object, MCPClient]:
        cluster = selected_cluster(application.state.clusters, cluster_id)
        return cluster, MCPClient(
            cluster.mcp_url, configuration.mcp_timeout_seconds
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def ready() -> dict:
        def check_mcp() -> str:
            try:
                _, client = mcp_for("kkbtest")
                client.list_tools()
                return "available"
            except (MCPUnavailable, UnknownClusterError):
                return "unavailable"

        def check_llm() -> str:
            try:
                application.state.llm_client.check()
                return "available"
            except LLMUnavailable:
                return "unavailable"

        # Dependency checks run concurrently so readiness is bounded by the
        # longest configured network timeout rather than their sum.
        with ThreadPoolExecutor(max_workers=2) as executor:
            mcp_check = executor.submit(check_mcp)
            llm_check = executor.submit(check_llm)
            dependencies = {"mcp": mcp_check.result(), "llm": llm_check.result()}
        status = "ready" if all(
            value == "available" for value in dependencies.values()
        ) else "degraded"
        return {"status": status, "dependencies": dependencies}

    @application.get("/api/v1/clusters")
    def clusters() -> dict:
        return {"clusters": [
            cluster.public_dict()
            for cluster in application.state.clusters.values()
        ]}

    @application.get("/api/v1/mcp/status")
    def mcp_status(cluster: str = Query(default="kkbtest")) -> JSONResponse:
        try:
            selected, client = mcp_for(cluster)
            tools = client.list_tools()
            return JSONResponse({
                "cluster": selected.id, "status": "available",
                "tool_count": len(tools),
            })
        except UnknownClusterError:
            return JSONResponse({"error": "unknown_cluster"}, status_code=404)
        except MCPUnavailable:
            return JSONResponse({
                "cluster": cluster, "status": "unavailable",
            }, status_code=503)

    @application.get("/api/v1/mcp/tools")
    def mcp_tools(cluster: str = Query(default="kkbtest")) -> JSONResponse:
        try:
            selected, client = mcp_for(cluster)
            tools = client.list_tools()
            return JSONResponse({
                "cluster": selected.id,
                "tools": [str(tool.get("name")) for tool in tools if tool.get("name")],
            })
        except UnknownClusterError:
            return JSONResponse({"error": "unknown_cluster"}, status_code=404)
        except MCPUnavailable:
            return JSONResponse({"error": "mcp_unavailable"}, status_code=503)

    @application.post("/api/v1/chat")
    def chat(payload: ChatRequest) -> JSONResponse:
        try:
            selected_cluster(application.state.clusters, payload.cluster)
        except UnknownClusterError:
            return JSONResponse({"error": "unknown_cluster"}, status_code=404)
        try:
            answer = application.state.llm_client.chat(payload.message)
            return JSONResponse({"cluster": payload.cluster, "answer": answer})
        except LLMUnavailable:
            return JSONResponse({"error": "llm_unavailable"}, status_code=503)

    return application


app = create_app()
