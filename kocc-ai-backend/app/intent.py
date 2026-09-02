from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from app.conversation import ConversationContext
from app.llm_client import LLMClient, LLMUnavailable
from app.namespace_inventory import NamespaceQuery


logger = logging.getLogger("kocc_ai.intent")

IntentMode = Literal[
    "conversational", "operational", "followup", "clarification_response"
]
ResourceKind = Literal[
    "Namespace", "Pod", "Node", "ClusterOperator", "Cluster"
]
Operation = Literal["list", "count", "exists", "status", "health", "metrics", "inspect"]
Reference = Literal["active_entity", "previous_query", "none"]
ResponseMode = Literal["concise", "normal", "detailed", "table"]

ALLOWED_MODES = frozenset({"conversational", "operational", "followup", "clarification_response"})
ALLOWED_RESOURCES = frozenset({"Namespace", "Pod", "Node", "ClusterOperator", "Cluster"})
ALLOWED_OPERATIONS = frozenset({"list", "count", "exists", "status", "health", "metrics", "inspect"})
ALLOWED_QUERY_TYPES = frozenset({"exact", "prefix", "contains", "total"})
ALLOWED_REFERENCES = frozenset({"active_entity", "previous_query", "none"})
ALLOWED_CLUSTERS = frozenset({"kkbtest", "rmtest", "all"})
ALLOWED_RESPONSE_MODES = frozenset({"concise", "normal", "detailed", "table"})
NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")

NLU_SYSTEM_PROMPT = """Classify one Turkish operations message. Return one JSON object only.
You do not answer the question and have no tools or live cluster facts.
Allowed keys/values:
mode=conversational|operational|followup|clarification_response;
resource_kind=Namespace|Pod|Node|ClusterOperator|Cluster|null;
operation=list|count|exists|status|health|metrics|inspect|null;
entity_text=DNS-like resource name|null;
query_type=exact|prefix|contains|total|null;
query_value=DNS-like text|null;
reference=active_entity|previous_query|none;
cluster_ref=kkbtest|rmtest|all|null;
response_mode=concise|normal|detailed|table|null.
Never invent a name or cluster. Use active_entity only when the message is anaphoric."""


@dataclass(frozen=True)
class StructuredIntent:
    mode: IntentMode
    resource_kind: ResourceKind | None = None
    operation: Operation | None = None
    entity_text: str | None = None
    query_type: str | None = None
    query_value: str | None = None
    reference: Reference = "none"
    cluster_ref: str | None = None
    response_mode: ResponseMode | None = None

    def namespace_query(self, context: ConversationContext) -> NamespaceQuery | None:
        if self.resource_kind != "Namespace":
            return None
        if self.reference == "active_entity":
            if context.active_entity_kind != "Namespace" or not context.active_entity_name:
                return None
            return NamespaceQuery("exact", context.active_entity_name)
        if self.reference == "previous_query":
            if context.last_resource_kind != "Namespace" or not context.last_filter_type:
                return None
            value = context.last_filter_value or ""
            return NamespaceQuery(
                context.last_filter_type, value, self.operation == "list"
            )
        query_type = self.query_type
        value = self.query_value or self.entity_text or ""
        if query_type in ALLOWED_QUERY_TYPES:
            return NamespaceQuery(query_type, value, self.operation == "list")
        if self.entity_text and self.operation in {"exists", "status", "health", "inspect"}:
            return NamespaceQuery("exact", self.entity_text)
        return None


def _normalize(value: str) -> str:
    folded = unicodedata.normalize(
        "NFKD", value.casefold().replace("’", "'").replace("ı", "i")
    )
    return " ".join("".join(
        character for character in folded if not unicodedata.combining(character)
    ).split())


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    return candidate if NAME_PATTERN.fullmatch(candidate) else None


def natural_namespace_intent(
    message: str, context: ConversationContext,
) -> StructuredIntent | None:
    """Resolve broad Namespace grammar without making factual decisions."""
    normalized = _normalize(message).strip(" ?.!")
    tokens = normalized.replace("'", " ").split()
    active_reference = any(term in normalized for term in (
        "bunun", "bunda", "buna", "onun", "ona", "burada", "buradaki",
        "sunun", "suna", "bu namespace", "bu proje", "az onceki",
        "ne durumda bu", "bu nasil",
    ))
    if active_reference and context.active_entity_kind == "Namespace":
        if "pod" in normalized:
            return StructuredIntent("followup", "Pod", "inspect", reference="active_entity")
        if "event" in normalized:
            return StructuredIntent("followup", "Namespace", "inspect", reference="active_entity")
        return StructuredIntent("followup", "Namespace", "status", reference="active_entity")

    membership = re.search(
        r"\b([a-z0-9](?:[-a-z0-9.]*[a-z0-9])?)\s+bunlarin\s+icinde\s+mi\b",
        normalized,
    )
    if membership:
        return StructuredIntent(
            "followup", "Namespace", "exists", membership.group(1),
            "exact", membership.group(1), "none",
        )

    if ("toplam" in tokens or "kac" in tokens) and any(
        token.startswith(("namespace", "proje", "project")) for token in tokens
    ):
        return StructuredIntent(
            "operational", "Namespace", "count", query_type="total"
        )

    prefix = re.search(
        r"\b([a-z0-9](?:[-a-z0-9.]*[a-z0-9])?)(?:\s+ile|la|le)\s+baslayan(?:lari|lar|leri)?\b",
        normalized,
    )
    if prefix:
        list_requested = any(term in normalized for term in (
            "liste", "doksene", "doksene", "neler", "goster",
        ))
        return StructuredIntent(
            "operational", "Namespace", "list" if list_requested else "count",
            query_type="prefix", query_value=prefix.group(1),
        )

    candidates = [token.strip("'.,?!") for token in tokens if "-" in token]
    candidate = next((item for item in candidates if _safe_name(item)), None)
    if candidate and any(term in normalized for term in (
        "var mi", "durum", "ne alemde", "saglik", "problem", "sorun",
        "bak", "kontrol", "gercekten",
    )):
        operation: Operation = "exists" if "var mi" in normalized else "status"
        return StructuredIntent(
            "operational", "Namespace", operation, candidate,
            "exact", candidate,
        )
    return None


def has_operational_reference(message: str, context: ConversationContext) -> bool:
    normalized = _normalize(message)
    resource_terms = (
        "namespace", "proje", "project", "pod", "node", "deployment",
        "service", "servis", "clusteroperator", "operator", "event", "cluster",
    )
    anaphora = (
        "bunun", "bunda", "buna", "sunun", "suna", "onun", "ona",
        "burada", "buradaki", "durumu ne", "ne durumda", "az onceki",
    )
    possible_name = any("-" in token and _safe_name(token.strip("'.,?!")) for token in normalized.split())
    contextual = bool(context.active_entity_name or context.last_resource_kind)
    return (
        any(term in normalized for term in resource_terms)
        or possible_name
        or contextual and any(term in normalized for term in anaphora)
    )


def _intent_from_dict(value: Any) -> StructuredIntent | None:
    if not isinstance(value, dict) or value.get("mode") not in ALLOWED_MODES:
        return None
    resource = value.get("resource_kind")
    operation = value.get("operation")
    query_type = value.get("query_type")
    reference = value.get("reference", "none")
    cluster = value.get("cluster_ref")
    response_mode = value.get("response_mode")
    if resource is not None and resource not in ALLOWED_RESOURCES:
        return None
    if operation is not None and operation not in ALLOWED_OPERATIONS:
        return None
    if query_type is not None and query_type not in ALLOWED_QUERY_TYPES:
        return None
    if reference not in ALLOWED_REFERENCES:
        return None
    if cluster is not None and cluster not in ALLOWED_CLUSTERS:
        return None
    if response_mode is not None and response_mode not in ALLOWED_RESPONSE_MODES:
        return None
    entity = _safe_name(value.get("entity_text"))
    query_value = _safe_name(value.get("query_value"))
    return StructuredIntent(
        value["mode"], resource, operation, entity, query_type, query_value,
        reference, cluster, response_mode,
    )


def interpret_intent(
    llm: LLMClient, message: str, context: ConversationContext,
) -> StructuredIntent | None:
    if not llm.is_configured():
        return None
    safe_state = {
        "active_clusters": list(context.active_cluster_ids),
        "active_entity": {
            "kind": context.active_entity_kind, "name": context.active_entity_name,
        },
        "previous_query": {
            "resource": context.last_resource_kind,
            "type": context.last_filter_type,
            "value": context.last_filter_value,
            "operation": context.last_operation,
        },
    }
    try:
        response = llm.chat_completion([
            {"role": "system", "content": NLU_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "message": message[:500], "context": safe_state,
            }, ensure_ascii=False)},
        ])
        content = response.get("content")
        if not isinstance(content, str):
            logger.warning("nlu_failure fallback=clarification")
            return None
        candidate = content.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate)
        intent = _intent_from_dict(json.loads(candidate))
        if intent is None:
            logger.warning("nlu_failure fallback=clarification")
            return None
        normalized_message = _normalize(message)
        if (
            intent.reference == "none"
            and any(value and value not in normalized_message for value in (
                intent.entity_text, intent.query_value,
            ))
        ):
            logger.warning("nlu_failure fallback=clarification")
            return None
        if (
            intent.cluster_ref not in {None, "all"}
            and intent.cluster_ref not in normalized_message
            and intent.cluster_ref not in context.active_cluster_ids
        ):
            logger.warning("nlu_failure fallback=clarification")
            return None
        logger.info(
            "intent_resolution source=llm mode=%s resource=%s",
            intent.mode, intent.resource_kind or "none",
        )
        return intent
    except (LLMUnavailable, json.JSONDecodeError, TypeError):
        logger.warning("nlu_failure fallback=clarification")
        return None
