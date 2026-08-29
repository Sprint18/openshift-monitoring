from __future__ import annotations

import logging
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent import AgentLimitReached, AgentLoop, can_run_without_llm
from app.clusters import (
    UnknownClusterError, cluster_registry,
    resolve_cluster_request, selected_cluster, validated_cluster_selection,
)
from app.config import Settings, load_settings
from app.llm_client import LLMClient, LLMUnavailable
from app.mcp_client import MCPClient, MCPUnavailable


logger = logging.getLogger("kocc_ai")
if not any(getattr(handler, "_kocc_ai_runtime", False) for handler in logger.handlers):
    runtime_handler = logging.StreamHandler(sys.stderr)
    runtime_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    runtime_handler._kocc_ai_runtime = True  # type: ignore[attr-defined]
    logger.addHandler(runtime_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class ChatRequest(BaseModel):
    cluster: str = "kkbtest"
    context_cluster_id: str | None = None
    target_cluster_ids: list[str] | None = None
    message: str = Field(min_length=1)


def _conversational_answer(message: str) -> str | None:
    normalized = " ".join(message.casefold().strip(" .!?").split())
    if normalized in {"merhaba", "selam", "hello", "hi", "teşekkürler", "tesekkurler"}:
        return "Merhaba, OpenShift operasyonları hakkında nasıl yardımcı olabilirim?"
    return None


def _attributed_answer(answer: str, cluster_name: str) -> str:
    legacy_heading = f"## {cluster_name}\n"
    trusted_heading = f"## Cluster: {cluster_name}\n"
    if answer.startswith(trusted_heading):
        return answer
    if answer.startswith(legacy_heading):
        return trusted_heading + answer[len(legacy_heading):]
    return f"## Cluster: {cluster_name}\n\n{answer}"


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
        chat_started = time.perf_counter()
        if len(payload.message) > configuration.agent_max_user_chars:
            return JSONResponse({"error": "message_too_large"}, status_code=400)
        resolved = resolve_cluster_request(
            payload.message, application.state.clusters
        )
        try:
            selected_scope = validated_cluster_selection(
                payload.target_cluster_ids, application.state.clusters
            )
        except UnknownClusterError:
            return JSONResponse({"error": "invalid_cluster_scope"}, status_code=400)
        if resolved is not None:
            scope = resolved.scope
            route_source = "explicit"
            operational_message = resolved.operational_message or payload.message
        elif selected_scope is not None:
            scope = selected_scope
            route_source = "selection"
            operational_message = payload.message
        else:
            conversational = _conversational_answer(payload.message)
            if conversational is not None:
                logger.info("ai_chat_scope scope=conversational")
                return JSONResponse({
                    "answer": conversational, "clusters": [],
                    "tool_calls": [], "evidence": [],
                })
            logger.info("ai_chat_scope scope=clarification")
            return JSONResponse({
                "answer": "Bu sorguyu hangi cluster için çalıştırayım?",
                "needs_cluster_selection": True,
                "cluster_choices": [
                    {"id": cluster.id, "name": cluster.name}
                    for cluster in application.state.clusters.values()
                    if cluster.enabled
                ],
                "allow_all": True,
            })
        if scope.kind == "single":
            logger.info(
                "ai_chat_scope scope=single target_clusters=%s source=%s",
                scope.cluster_ids[0], route_source,
            )
        else:
            logger.info(
                "ai_chat_scope scope=%s target_clusters=%s source=%s",
                "multi" if scope.kind == "multiple" else scope.kind,
                ",".join(scope.cluster_ids), route_source,
            )
        if (
            not application.state.llm_client.is_configured()
            and not can_run_without_llm(operational_message)
        ):
            logger.warning(
                "ai_chat_failure target_clusters=%s reason=llm_unavailable duration_ms=%s",
                ",".join(scope.cluster_ids),
                round((time.perf_counter() - chat_started) * 1000),
            )
            return JSONResponse({
                "error": "llm_unavailable",
                "message": "ShiftLight AI şu anda kullanılamıyor.",
            }, status_code=503)

        def execute(cluster_id: str) -> dict:
            selected, mcp_client = mcp_for(cluster_id)
            started = time.perf_counter()
            try:
                result = AgentLoop(
                    configuration, application.state.llm_client, mcp_client,
                    selected.id, selected.name,
                ).run(operational_message)
                evidence = [
                    {"cluster": selected.id, **item}
                    for item in result.evidence
                ]
                tool_summary = ",".join(
                    f"{item['name']}:{item['status']}"
                    for item in result.tool_calls
                ) or "none"
                logger.info(
                    "ai_chat_complete cluster=%s outcome=success scope=%s tools=%s facts=%s iterations=%s duration_ms=%s",
                    selected.id, scope.kind, tool_summary,
                    any("facts" in item for item in evidence), result.iterations,
                    round((time.perf_counter() - started) * 1000),
                )
                return {
                    "cluster": selected.id, "name": selected.name,
                    "status": "success", "answer": result.answer,
                    "tool_calls": result.tool_calls, "evidence": evidence,
                }
            except MCPUnavailable:
                error = "mcp_unavailable"
            except LLMUnavailable:
                error = "llm_unavailable"
            except AgentLimitReached as exc:
                error = (
                    "agent_tool_call_limit" if str(exc) == "tool_call_limit"
                    else "agent_iteration_limit"
                )
            except Exception:
                error = "internal_error"
            logger.warning(
                "ai_chat_failure target_clusters=%s reason=%s duration_ms=%s",
                selected.id, error,
                round((time.perf_counter() - started) * 1000),
            )
            return {
                "cluster": selected.id, "name": selected.name,
                "status": "unavailable", "error": error,
            }

        outcomes = [execute(cluster_id) for cluster_id in scope.cluster_ids]
        if scope.kind == "single":
            outcome = outcomes[0]
            if outcome["status"] != "success":
                return JSONResponse({
                    "error": outcome["error"],
                    "message": f"{outcome['name']} cluster verisi şu anda kullanılamıyor.",
                }, status_code=503)
            return JSONResponse({
                "cluster": outcome["cluster"],
                "clusters": [{"id": outcome["cluster"], "name": outcome["name"]}],
                "answer": _attributed_answer(outcome["answer"], outcome["name"]),
                "tool_calls": outcome["tool_calls"],
                "evidence": outcome["evidence"],
            })

        successful = [item for item in outcomes if item["status"] == "success"]
        if not successful:
            return JSONResponse(
                {"error": "multi_cluster_unavailable"}, status_code=503
            )
        sections = []
        for outcome in outcomes:
            if outcome["status"] == "success":
                body = outcome["answer"]
                sections.append(
                    body if body.startswith(f"## {outcome['name']}\n")
                    else f"## {outcome['name']}\n\n{body}"
                )
            else:
                sections.append(
                    f"## {outcome['name']}\n\n"
                    "Cluster verisi şu anda kullanılamıyor."
                )
        return JSONResponse({
            "cluster": "all" if scope.kind == "all" else "multiple",
            "clusters": [
                {"id": item["cluster"], "name": item["name"]}
                for item in outcomes
            ],
            "answer": "\n\n".join(sections),
            "tool_calls": [
                {"cluster": item["cluster"], **tool}
                for item in successful for tool in item["tool_calls"]
            ],
            "evidence": [
                evidence for item in successful for evidence in item["evidence"]
            ],
        })

    return application


app = create_app()
