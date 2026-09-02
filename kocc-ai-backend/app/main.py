from __future__ import annotations

import logging
import sys
import time
import uuid
from dataclasses import replace
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent import AgentLimitReached, AgentLoop, can_run_without_llm
from app.classification import classify_conversation
from app.clusters import (
    ClusterScope, UnknownClusterError, cluster_registry,
    conversation_scope_selection, resolve_cluster_request, selected_cluster,
    validated_cluster_selection,
)
from app.config import Settings, load_settings
from app.conversation import (
    ConversationContext, bounded_history, confirmation_value,
    context_for_namespace_result, contextual_namespace_query,
    contextual_entity_message, conversational_response,
    namespace_query_from_context, namespace_query_message,
    render_namespace_answer, safe_conversation_summary,
)
from app.llm_client import LLMClient, LLMUnavailable
from app.k8s_client import KubernetesAPIAdapter
from app.intent import (
    StructuredIntent, has_operational_reference, interpret_intent,
    natural_namespace_intent,
)
from app.mcp_client import MCPClient, MCPUnavailable
from app.namespace_inventory import (
    NamespaceQuery, execute_namespace_query, parse_namespace_query,
)


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
    conversation_scope: str = "auto"
    message: str = Field(min_length=1)
    recent_turns: list[dict[str, Any]] = Field(default_factory=list)
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    conversation_summary: str = ""


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
        # Readiness is intentionally local and side-effect free. Remote MCP/LLM
        # availability remains request-time controlled and explicit diagnostics
        # are available under /api/v1/mcp/status.
        dependencies = {
            "mcp": "configured" if all(
                cluster.mcp_url for cluster in application.state.clusters.values()
                if cluster.enabled
            ) else "unconfigured",
            "llm": (
                "configured" if application.state.llm_client.is_configured()
                else "unconfigured"
            ),
        }
        status = "ready" if all(
            value == "configured" for value in dependencies.values()
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
        if payload.target_cluster_ids is not None and (
            not payload.target_cluster_ids
            or any(
                cluster_id not in application.state.clusters
                or not application.state.clusters[cluster_id].enabled
                for cluster_id in payload.target_cluster_ids
            )
        ):
            return JSONResponse({"error": "invalid_cluster_scope"}, status_code=400)
        if payload.conversation_scope not in {
            "auto", "all", *application.state.clusters.keys()
        }:
            return JSONResponse({"error": "invalid_cluster_scope"}, status_code=400)
        history = bounded_history(payload.recent_turns)
        conversation_summary = safe_conversation_summary(
            payload.conversation_summary
        )
        if conversation_summary:
            logger.info("conversation_summary action=received bounded=true")
        conversation_context = ConversationContext.from_payload(
            payload.conversation_context
        )
        forced_namespace_query = contextual_namespace_query(
            payload.message, conversation_context
        )
        forced_entity_message = contextual_entity_message(
            payload.message, conversation_context
        )
        deterministic_intent = natural_namespace_intent(
            payload.message, conversation_context
        )
        interpreted_namespace_query = (
            parse_namespace_query(payload.message)
            or (deterministic_intent.namespace_query(conversation_context)
                if deterministic_intent is not None else None)
        )
        grounding_required = has_operational_reference(
            payload.message, conversation_context
        )
        classification = classify_conversation(payload.message)
        nlu_intent: StructuredIntent | None = None
        if (
            interpreted_namespace_query is None
            and forced_namespace_query is None
            and forced_entity_message is None
            and grounding_required
            and classification.conversation_class == "conversational"
        ):
            nlu_intent = interpret_intent(
                application.state.llm_client, payload.message,
                conversation_context,
            )
            if nlu_intent is not None:
                interpreted_namespace_query = nlu_intent.namespace_query(
                    conversation_context
                )
                if (
                    nlu_intent.reference == "active_entity"
                    and nlu_intent.resource_kind in {"Pod", "Namespace"}
                    and conversation_context.active_entity_kind == "Namespace"
                    and conversation_context.active_entity_name
                ):
                    if nlu_intent.resource_kind == "Pod":
                        forced_entity_message = (
                            f"{conversation_context.active_entity_name} namespace "
                            "podlarını incele"
                        )
                    elif interpreted_namespace_query is None:
                        interpreted_namespace_query = NamespaceQuery(
                            "exact", conversation_context.active_entity_name
                        )
        if deterministic_intent is not None:
            logger.info(
                "intent_resolution source=deterministic mode=%s confidence=high",
                deterministic_intent.mode,
            )
        accepted_suggestion = False
        confirmation = confirmation_value(payload.message)
        if conversation_context.pending_suggestion_name and confirmation is not None:
            if confirmation:
                forced_namespace_query = NamespaceQuery(
                    "exact", conversation_context.pending_suggestion_name
                )
                accepted_suggestion = True
                logger.info(
                    "suggestion_resolution type=namespace accepted=true cluster_id=%s",
                    conversation_context.active_cluster_ids[0]
                    if conversation_context.active_cluster_ids else "unknown",
                )
            else:
                cleared = conversation_context.without_pending_suggestion()
                logger.info("suggestion_resolution type=namespace accepted=false")
                return JSONResponse({
                    "answer": "Tamam, öneriyi kullanmayacağım.", "clusters": [],
                    "tool_calls": [], "evidence": [],
                    "conversation_context": cleared.public_dict(),
                })
        elif conversation_context.pending_suggestion_name:
            conversation_context = conversation_context.without_pending_suggestion()
        resolved = resolve_cluster_request(
            payload.message, application.state.clusters
        )
        if (
            classification.conversation_class == "conversational"
            and forced_namespace_query is None
            and forced_entity_message is None
            and interpreted_namespace_query is None
            and not grounding_required
            and resolved is None
        ):
            logger.info(
                "ai_chat_scope scope=conversational subtype=%s",
                classification.subtype,
            )
            return JSONResponse({
                "answer": conversational_response(
                    application.state.llm_client, classification,
                    payload.message, history,
                    conversation_summary,
                ), "clusters": [],
                "tool_calls": [], "evidence": [],
                "conversation_context": conversation_context.public_dict(),
            })
        try:
            selected_scope = validated_cluster_selection(
                payload.target_cluster_ids, application.state.clusters
            )
            conversation_decision = conversation_scope_selection(
                payload.conversation_scope, application.state.clusters
            )
        except UnknownClusterError:
            return JSONResponse({"error": "invalid_cluster_scope"}, status_code=400)
        if resolved is not None:
            scope = resolved.scope
            route_source = "explicit"
            operational_message = resolved.operational_message or payload.message
            if (
                forced_namespace_query is None
                and resolved.operational_message.strip().casefold().strip(" ?.!")
                in {"", "peki"}
            ):
                forced_namespace_query = namespace_query_from_context(
                    conversation_context
                )
        elif selected_scope is not None:
            scope = selected_scope
            route_source = "clarification"
            operational_message = payload.message
        elif conversation_decision.scope is not None:
            scope = conversation_decision.scope
            route_source = conversation_decision.source
            operational_message = payload.message
        elif (
            (forced_namespace_query is not None or forced_entity_message is not None
             or interpreted_namespace_query is not None)
            and conversation_context.active_cluster_ids
        ):
            scope = ClusterScope(
                "single" if len(conversation_context.active_cluster_ids) == 1
                else "multiple",
                conversation_context.active_cluster_ids,
            )
            route_source = "conversation_context"
            operational_message = forced_entity_message or payload.message
            logger.info(
                "context_followup resolved=true resource=Namespace cluster_id=%s",
                ",".join(scope.cluster_ids),
            )
        elif grounding_required and conversation_context.active_cluster_ids:
            logger.info(
                "grounding_firewall conversational_blocked=true reason=operational_reference"
            )
            entity = conversation_context.active_entity_name
            return JSONResponse({
                "answer": (
                    f"Burada `{entity}` namespace'ini mi kastediyorsun?"
                    if entity else "Hangi kaynağı kastettiğini netleştirir misin?"
                ),
                "clusters": [], "tool_calls": [], "evidence": [],
                "conversation_context": conversation_context.public_dict(),
            })
        else:
            logger.info("ai_chat_scope scope=clarification")
            pending_context = (
                context_for_namespace_result(
                    conversation_context, (), interpreted_namespace_query, {},
                ) if interpreted_namespace_query is not None
                else conversation_context
            )
            if interpreted_namespace_query is not None:
                logger.info(
                    "context_state action=pending resource=Namespace query_type=%s operation=%s",
                    interpreted_namespace_query.mode,
                    "list" if interpreted_namespace_query.list_names else "count",
                )
            return JSONResponse({
                "answer": "Bu sorguyu hangi cluster veya clusterlar için çalıştırayım?",
                "needs_cluster_selection": True,
                "clarification_id": str(uuid.uuid4()),
                "cluster_choices": [
                    {"id": cluster.id, "name": cluster.name}
                    for cluster in application.state.clusters.values()
                    if cluster.enabled
                ],
                "allow_all": True,
                "conversation_context": pending_context.public_dict(),
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
        resolved_namespace_query = (
            forced_namespace_query
            or parse_namespace_query(operational_message)
            or interpreted_namespace_query
        )
        if (
            not application.state.llm_client.is_configured()
            and not can_run_without_llm(operational_message)
            and not (
                resolved_namespace_query is not None
                and any(
                    application.state.clusters[cluster_id].kubernetes_api.enabled
                    for cluster_id in scope.cluster_ids
                )
            )
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
            selected = selected_cluster(application.state.clusters, cluster_id)
            started = time.perf_counter()
            try:
                namespace_query = resolved_namespace_query
                if namespace_query is not None and selected.kubernetes_api.enabled:
                    result = execute_namespace_query(
                        KubernetesAPIAdapter(
                            cluster_id=selected.id,
                            timeout_seconds=configuration.k8s_timeout_seconds,
                            page_limit=configuration.k8s_page_limit,
                            max_pages=configuration.k8s_max_pages,
                            max_items=configuration.k8s_max_items,
                        ),
                        selected.id,
                        namespace_query,
                    )
                    facts = (
                        result.evidence_items[0].facts
                        if result.evidence_items else {}
                    )
                    completeness = (
                        result.evidence_items[0].completeness
                        if result.evidence_items else "unavailable"
                    )
                    canonical_answer = render_namespace_answer(
                        result.answer, selected.name, namespace_query,
                        facts, completeness,
                    )
                    synthesized_answer = canonical_answer
                    if (
                        "bunların içinde" in payload.message.casefold()
                        and namespace_query.mode == "exact"
                        and conversation_context.last_filter_type in {"prefix", "contains"}
                        and facts.get("exists") is True
                    ):
                        filter_value = conversation_context.last_filter_value or ""
                        belongs = (
                            namespace_query.value.startswith(filter_value)
                            if conversation_context.last_filter_type == "prefix"
                            else filter_value in namespace_query.value
                        )
                        synthesized_answer = (
                            f"Evet, `{namespace_query.value}` bu grupta yer alıyor."
                            if belongs else
                            f"`{namespace_query.value}` mevcut, ancak önceki grupta yer almıyor."
                        )
                    next_context = context_for_namespace_result(
                        conversation_context, (selected.id,), namespace_query, facts
                    )
                    logger.info(
                        "context_state action=updated cluster_id=%s resource=Namespace query_type=%s operation=%s",
                        selected.id, namespace_query.mode,
                        next_context.last_operation,
                    )
                    if next_context.active_entity_name != conversation_context.active_entity_name:
                        logger.info(
                            "active_entity action=set kind=Namespace cluster_id=%s",
                            selected.id,
                        )
                    if accepted_suggestion and facts.get("exists") is True:
                        synthesized_answer = (
                            f"Tamam, `{namespace_query.value}` namespace'ini "
                            "baz alıyorum.\n\n" + synthesized_answer
                        )
                    result = replace(result, answer=synthesized_answer)
                else:
                    _, mcp_client = mcp_for(cluster_id)
                    result = AgentLoop(
                        configuration, application.state.llm_client, mcp_client,
                        selected.id, selected.name,
                    ).run(
                        namespace_query_message(namespace_query)
                        if namespace_query is not None else operational_message
                    )
                    next_context = (
                        context_for_namespace_result(
                            conversation_context, (selected.id,),
                            namespace_query, {},
                        ) if namespace_query is not None
                        else conversation_context.with_active_clusters((selected.id,))
                    )
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
                    "conversation_context": next_context.public_dict(),
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
                "conversation_context": outcome["conversation_context"],
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
            "conversation_context": (
                successful[0]["conversation_context"]
                if len(successful) == 1 else ConversationContext(
                    active_cluster_ids=tuple(
                        item["cluster"] for item in successful
                    )
                ).public_dict()
            ),
        })

    return application


app = create_app()
