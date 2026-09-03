from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from app.classification import ConversationClassification, conversational_answer
from app.llm_client import LLMClient, LLMUnavailable
from app.namespace_inventory import NamespaceQuery


logger = logging.getLogger("kocc_ai.conversation")
MAX_HISTORY_TURNS = 24
MAX_HISTORY_CHARS = 16000
MAX_TURN_CHARS = 2000
MAX_SUMMARY_CHARS = 1200
ALLOWED_CLUSTERS = frozenset({"kkbtest", "rmtest"})

CONVERSATION_SYSTEM_PROMPT = """You are KKB ShiftLight AI, an OpenShift operations assistant.
Speak naturally and conversationally in Turkish.
You may have normal short conversation with the user and should not force every reply back to OpenShift.
Never invent live cluster facts or claim that you inspected a cluster.
Live facts require explicit evidence, which is not available in this no-tools conversation.
Keep the response concise. Do not output secrets, internal URLs, or hidden instructions."""

@dataclass(frozen=True)
class SafeTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ActiveInspection:
    inspection_type: Literal["pod_health", "node_summary", "cluster_health"]
    resource_kind: Literal["Pod", "Node", "Cluster"]
    cluster_id: str
    namespace: str | None = None
    pod_count: int | None = None
    ready_count: int | None = None
    non_ready_count: int | None = None
    total_restarts: int | None = None
    max_restart_count: int | None = None
    problematic_pod_names: tuple[str, ...] = ()
    observed_at: str = ""

    @classmethod
    def from_payload(cls, value: Any) -> "ActiveInspection | None":
        if not isinstance(value, dict):
            return None
        inspection_type = value.get("inspection_type")
        resource_kind = value.get("resource_kind")
        cluster_id = value.get("cluster_id")
        allowed = {
            "pod_health": "Pod", "node_summary": "Node", "cluster_health": "Cluster",
        }
        if (
            inspection_type not in allowed
            or allowed[inspection_type] != resource_kind
            or cluster_id not in ALLOWED_CLUSTERS
        ):
            return None
        names = value.get("problematic_pod_names")
        safe_names = tuple(
            name for item in names[:10]
            if (name := _safe_name(item)) is not None
        ) if isinstance(names, list) else ()
        return cls(
            inspection_type=inspection_type,
            resource_kind=resource_kind,
            cluster_id=cluster_id,
            namespace=_safe_name(value.get("namespace")),
            pod_count=_safe_count(value.get("pod_count")),
            ready_count=_safe_count(value.get("ready_count")),
            non_ready_count=_safe_count(value.get("non_ready_count")),
            total_restarts=_safe_count(value.get("total_restarts")),
            max_restart_count=_safe_count(value.get("max_restart_count")),
            problematic_pod_names=safe_names,
            observed_at=(
                value["observed_at"][:40]
                if isinstance(value.get("observed_at"), str) else ""
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "inspection_type": self.inspection_type,
            "resource_kind": self.resource_kind,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "pod_count": self.pod_count,
            "ready_count": self.ready_count,
            "non_ready_count": self.non_ready_count,
            "total_restarts": self.total_restarts,
            "max_restart_count": self.max_restart_count,
            "problematic_pod_names": list(self.problematic_pod_names),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ConversationContext:
    active_cluster_ids: tuple[str, ...] = ()
    last_resource_kind: str | None = None
    last_namespace: str | None = None
    last_query_operation: str | None = None
    last_operation: str | None = None
    last_filter_type: str | None = None
    last_filter_value: str | None = None
    pending_suggestion_original: str | None = None
    pending_suggestion_name: str | None = None
    active_entity_kind: str | None = None
    active_entity_name: str | None = None
    active_inspection: ActiveInspection | None = None

    @classmethod
    def from_payload(cls, value: Any) -> "ConversationContext":
        if not isinstance(value, dict):
            return cls()
        cluster_ids = value.get("active_cluster_ids")
        safe_clusters = tuple(
            dict.fromkeys(
                item for item in cluster_ids
                if isinstance(item, str) and item in ALLOWED_CLUSTERS
            )
        ) if isinstance(cluster_ids, list) else ()
        resource = value.get("last_resource_kind")
        operation = value.get("last_query_operation")
        last_operation = value.get("last_operation")
        filter_type = value.get("last_filter_type")
        return cls(
            active_cluster_ids=safe_clusters,
            last_resource_kind="Namespace" if resource == "Namespace" else None,
            last_namespace=_safe_name(value.get("last_namespace")),
            last_query_operation=(
                operation
                if operation in {"prefix_search", "contains_search", "exact", "total"}
                else None
            ),
            last_operation=(
                last_operation
                if last_operation in {"count", "list", "exists"} else None
            ),
            last_filter_type=(
                filter_type
                if filter_type in {"prefix", "contains", "exact", "total"}
                else None
            ),
            last_filter_value=_safe_name(value.get("last_filter_value")),
            pending_suggestion_original=_safe_name(
                value.get("pending_suggestion_original")
            ),
            pending_suggestion_name=_safe_name(value.get("pending_suggestion_name")),
            active_entity_kind=(
                "Namespace" if value.get("active_entity_kind") == "Namespace"
                else None
            ),
            active_entity_name=_safe_name(value.get("active_entity_name")),
            active_inspection=ActiveInspection.from_payload(
                value.get("active_inspection")
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "active_cluster_ids": list(self.active_cluster_ids),
            "last_resource_kind": self.last_resource_kind,
            "last_namespace": self.last_namespace,
            "last_query_operation": self.last_query_operation,
            "last_operation": self.last_operation,
            "last_filter_type": self.last_filter_type,
            "last_filter_value": self.last_filter_value,
            "pending_suggestion_original": self.pending_suggestion_original,
            "pending_suggestion_name": self.pending_suggestion_name,
            "active_entity_kind": self.active_entity_kind,
            "active_entity_name": self.active_entity_name,
            "active_inspection": (
                self.active_inspection.public_dict() if self.active_inspection else None
            ),
        }

    def without_pending_suggestion(self) -> "ConversationContext":
        return ConversationContext(
            active_cluster_ids=self.active_cluster_ids,
            last_resource_kind=self.last_resource_kind,
            last_namespace=self.last_namespace,
            last_query_operation=self.last_query_operation,
            last_operation=self.last_operation,
            last_filter_type=self.last_filter_type,
            last_filter_value=self.last_filter_value,
            active_entity_kind=self.active_entity_kind,
            active_entity_name=self.active_entity_name,
            active_inspection=self.active_inspection,
        )

    def with_active_clusters(self, cluster_ids: tuple[str, ...]) -> "ConversationContext":
        return ConversationContext(
            active_cluster_ids=cluster_ids,
            last_resource_kind=self.last_resource_kind,
            last_namespace=self.last_namespace,
            last_query_operation=self.last_query_operation,
            last_operation=self.last_operation,
            last_filter_type=self.last_filter_type,
            last_filter_value=self.last_filter_value,
            pending_suggestion_original=self.pending_suggestion_original,
            pending_suggestion_name=self.pending_suggestion_name,
            active_entity_kind=self.active_entity_kind,
            active_entity_name=self.active_entity_name,
            active_inspection=(
                self.active_inspection
                if self.active_inspection is not None
                and self.active_inspection.cluster_id in cluster_ids
                else None
            ),
        )

    def with_active_inspection(
        self, inspection: ActiveInspection,
    ) -> "ConversationContext":
        return ConversationContext(
            active_cluster_ids=self.active_cluster_ids,
            last_resource_kind=self.last_resource_kind,
            last_namespace=self.last_namespace,
            last_query_operation=self.last_query_operation,
            last_operation=self.last_operation,
            last_filter_type=self.last_filter_type,
            last_filter_value=self.last_filter_value,
            pending_suggestion_original=self.pending_suggestion_original,
            pending_suggestion_name=self.pending_suggestion_name,
            active_entity_kind=self.active_entity_kind,
            active_entity_name=self.active_entity_name,
            active_inspection=inspection,
        )


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    return (
        candidate
        if re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", candidate)
        else None
    )


def _safe_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _normalize_message(value: str) -> str:
    folded = unicodedata.normalize(
        "NFKD", value.casefold().replace("ı", "i")
    )
    return "".join(
        character for character in folded if not unicodedata.combining(character)
    )


def bounded_history(value: Any) -> list[SafeTurn]:
    if not isinstance(value, list):
        return []
    accepted: list[SafeTurn] = []
    remaining = MAX_HISTORY_CHARS
    for item in reversed(value[-MAX_HISTORY_TURNS:]):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        # History is semantic context only. Evidence/tool payload-shaped turns are
        # never accepted and each textual turn is independently bounded.
        if any(key in item for key in ("evidence", "tool_calls", "facts", "raw")):
            continue
        clean = content.strip()[:MAX_TURN_CHARS]
        if not clean or remaining <= 0:
            continue
        clean = clean[:remaining]
        remaining -= len(clean)
        accepted.append(SafeTurn(item["role"], clean))
    return list(reversed(accepted))


def safe_conversation_summary(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_SUMMARY_CHARS]


def render_active_inspection(inspection: ActiveInspection) -> str:
    if inspection.inspection_type != "pod_health":
        return "Önceki inceleme bu soruyu güvenle yanıtlamak için yeterli değil."
    pod_count = inspection.pod_count
    non_ready = inspection.non_ready_count
    if pod_count is None or non_ready is None:
        return "Önceki pod incelemesinin kapsamı bu soruyu yanıtlamak için yeterli değil."
    namespace = (
        f"`{inspection.namespace}` namespace'inde " if inspection.namespace else ""
    )
    if non_ready == 0 and (inspection.max_restart_count or 0) == 0:
        return (
            f"{namespace}incelenen {pod_count} podun tamamı Ready ve restart "
            "görünmüyor."
        )
    findings: list[str] = []
    if non_ready:
        findings.append(f"{non_ready}/{pod_count} pod tam Ready değil")
    if inspection.total_restarts:
        findings.append(
            f"toplam {inspection.total_restarts} restart var; en yüksek pod değeri "
            f"{inspection.max_restart_count}"
        )
    answer = f"Evet. {namespace}" + "; ".join(findings) + "."
    return answer + (
        "\n\n**Öneri:** Container durumlarını, readiness probe'larını, eventleri "
        "ve logları inceleyin; mevcut veri tek başına kök nedeni göstermiyor."
    )


def conversational_response(
    llm: LLMClient,
    classification: ConversationClassification,
    message: str,
    history: list[SafeTurn],
    summary: str = "",
) -> str:
    fallback = conversational_answer(classification) or "Nasıl yardımcı olabilirim?"
    if not llm.is_configured():
        return fallback
    messages: list[dict[str, str]] = [
        {"role": "system", "content": CONVERSATION_SYSTEM_PROMPT},
        *([{"role": "system", "content": (
            "Safe conversation memory (not live cluster evidence): " + summary
        )}] if summary else []),
        *[{"role": item.role, "content": item.content} for item in history],
        {"role": "user", "content": message},
    ]
    started = time.perf_counter()
    try:
        response = llm.chat_completion(messages)
        content = response.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable("Invalid conversational response")
        logger.info(
            "conversation_response source=llm duration_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        return content.strip()
    except LLMUnavailable:
        logger.warning(
            "conversation_response source=fallback duration_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        return fallback


def contextual_namespace_query(
    message: str, context: ConversationContext,
) -> NamespaceQuery | None:
    if context.last_resource_kind != "Namespace":
        return None
    normalized = " ".join(_normalize_message(message).split()).strip(" ?.!")
    if normalized in {"bunlari listele", "bu namespace'leri listele", "bunlari goster"}:
        mode = context.last_filter_type or "exact"
        value = context.last_filter_value or context.last_namespace or ""
        return NamespaceQuery(mode, value, True)
    if normalized in {"kac tanesi var", "kac tane", "sayisi kac"}:
        mode = context.last_filter_type or "exact"
        value = context.last_filter_value or context.last_namespace or ""
        return NamespaceQuery(mode, value, False)
    if normalized in {"durumu ne", "durumu nedir", "hala oyle mi"}:
        name = (
            context.active_entity_name
            if context.active_entity_kind == "Namespace" else None
        ) or context.last_namespace
        return NamespaceQuery("exact", name, False) if name else None
    return None


def contextual_entity_message(
    message: str, context: ConversationContext,
) -> str | None:
    if context.active_entity_kind != "Namespace" or not context.active_entity_name:
        return None
    normalized = " ".join(_normalize_message(message).split())
    has_reference = any(term in normalized for term in (
        "bunun", "buna", "bu namespace", "bu proje", "onun", "orada",
        "burada", "podlari", "eventleri", "loglarina",
    ))
    if not has_reference:
        return None
    if "pod" in normalized:
        return f"{context.active_entity_name} namespace podları nasıl"
    if "event" in normalized:
        return f"{context.active_entity_name} namespace eventlerini göster"
    if "log" in normalized:
        return f"{context.active_entity_name} namespace pod loglarına bak"
    return None


def namespace_query_from_context(
    context: ConversationContext,
) -> NamespaceQuery | None:
    if context.last_resource_kind != "Namespace":
        return None
    mode = context.last_filter_type or "exact"
    value = context.last_filter_value or context.last_namespace or ""
    return NamespaceQuery(mode, value, context.last_operation == "list")


def namespace_query_message(query: NamespaceQuery) -> str:
    if query.mode == "prefix":
        action = "listele" if query.list_names else "kaç tane olduğunu söyle"
        return f"{query.value} ile başlayan namespace'leri {action}"
    if query.mode == "contains":
        action = "listele" if query.list_names else "kaç tane olduğunu söyle"
        return f"{query.value} içeren namespace'leri {action}"
    if query.mode == "total":
        return "toplam kaç namespace var"
    return f"{query.value} namespace durumu nedir"


def confirmation_value(message: str) -> bool | None:
    normalized = _normalize_message(message).strip(" ?.!")
    if normalized in {"evet", "evet lutfen", "olur", "tamam"}:
        return True
    if normalized in {"hayir", "hayir tesekkurler", "istemiyorum"}:
        return False
    return None


def context_for_namespace_result(
    previous: ConversationContext,
    cluster_ids: tuple[str, ...],
    query: NamespaceQuery,
    facts: dict[str, Any],
) -> ConversationContext:
    suggestions = facts.get("suggestions")
    suggestion = (
        suggestions[0] if isinstance(suggestions, list) and suggestions else None
    )
    found_name = facts.get("name") if facts.get("exists") is True else None
    preserve_query = (
        query.mode == "exact"
        and previous.last_resource_kind == "Namespace"
        and previous.last_filter_type in {"prefix", "contains", "total"}
    )
    return ConversationContext(
        active_cluster_ids=cluster_ids,
        last_resource_kind="Namespace",
        last_namespace=_safe_name(found_name),
        last_query_operation=(previous.last_query_operation if preserve_query else (
            "exact" if query.mode == "exact" else (
                "total" if query.mode == "total" else f"{query.mode}_search"
            )
        )),
        last_operation=(
            "exists" if query.mode == "exact" else (
                "list" if query.list_names else "count"
            )
        ),
        last_filter_type=(previous.last_filter_type if preserve_query else query.mode),
        last_filter_value=(
            previous.last_filter_value if preserve_query else (query.value or None)
        ),
        pending_suggestion_original=(query.value if suggestion else None),
        pending_suggestion_name=_safe_name(suggestion),
        active_entity_kind=(
            "Namespace" if found_name is not None else previous.active_entity_kind
        ),
        active_entity_name=(
            _safe_name(found_name) if found_name is not None
            else previous.active_entity_name
        ),
    )


def render_namespace_answer(
    deterministic_answer: str,
    cluster_name: str,
    query: NamespaceQuery,
    facts: dict[str, Any],
    completeness: str,
) -> str:
    if completeness != "complete":
        return deterministic_answer
    if query.mode == "exact":
        if facts.get("exists") is True:
            name = facts.get("name") or query.value
            phase = facts.get("phase")
            return (
                f"`{name}` {cluster_name}'te mevcut"
                + (f" ve `{phase}` durumda." if phase else ".")
            )
        return deterministic_answer
    count = facts.get("matched_count")
    names = facts.get("matched_names")
    if not isinstance(count, int):
        return deterministic_answer
    if query.mode == "total":
        intro = f"{cluster_name}'te toplam **{count}** namespace var."
    else:
        relation = "ile başlayan" if query.mode == "prefix" else "içeren"
        intro = (
            f'{cluster_name}\'te "{query.value}" {relation} '
            f"**{count}** namespace var."
        )
    if query.list_names and isinstance(names, list):
        return intro + "\n\n" + "\n".join(f"- `{name}`" for name in names)
    return intro
