from __future__ import annotations

import re
from typing import Any


_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"
_HYPHENATED_DNS_LABEL = r"[a-z0-9]+(?:-[a-z0-9]+)+"
_GRAMMAR_TOKENS = frozenset({
    "a", "ait", "e", "egressip", "hangisi", "hepsi", "in", "ip", "namespace",
    "nedir", "nin", "nın", "ortamındaki", "tüm", "un", "ün",
})


def _namespace_candidate(value: str) -> str | None:
    """Accept a complete DNS token, never a suffix sliced from another word."""
    return value if value not in _GRAMMAR_TOKENS else None


def egressip_namespace(message: str) -> str | None:
    normalized = " ".join(message.casefold().replace("’", "'").split())
    if not re.search(r"\begress\s*ip\b", normalized):
        return None
    # Explicit namespace grammar wins. Every candidate starts at a whitespace/start
    # boundary and ends before a complete marker/apostrophe; the regex cannot
    # backtrack into `namespace'ine` and reinterpret its suffix as a namespace.
    patterns = (
        rf"(?:^|\s)({_DNS_LABEL})\s+(?:namespace|namesepace)(?:'(?:indeki|inde|ine|ien|inin|ının|unun|ünün))?\s+(?:(?:ait|için)\s+)?egress\s*ip\b",
        rf"(?:^|\s)(?:namespace|proje)\s+({_DNS_LABEL})(?:'(?:nin|nın|nun|nün))?.*?\begress\s*ip\b",
        rf"\begress\s*ip\b.*?\b(?:namespace|proje)\s+({_DNS_LABEL})(?=$|\s|[?,.!])",
        rf"(?:^|\s)({_DNS_LABEL})'(?:ye|ya|e|a)\s+ait\s+egress\s*ip\b",
        rf"(?:^|\s)({_HYPHENATED_DNS_LABEL})\s+egress\s*ip(?:'?(?:si|i))?\b",
        rf"(?:^|\s)({_DNS_LABEL})\s+hangi\s+egress\s*ip(?:'?(?:yi|i))?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match and (candidate := _namespace_candidate(match.group(1))):
            return candidate
    return None


def is_egressip_intent(message: str) -> bool:
    return re.search(
        r"\begress\s*ip(?:'?[a-zçğıöşü]+)?\b", message.casefold()
    ) is not None


def egressip_query_mode(message: str) -> str:
    """Classify direct EgressIP requests without involving the LLM."""
    if egressip_namespace(message) is not None:
        return "namespace"
    normalized = " ".join(message.casefold().replace("ı", "i").split())
    inventory_markers = (
        "tum", "tüm", "hepsi", "listele", "listesi", "liste",
        "cluster'daki", "clusterdaki", "all", "inventory", "show",
    )
    inventory = any(marker in normalized for marker in inventory_markers) or bool(
        re.search(r"\bhangi\b.*\b(?:var|mevcut)\b", normalized)
    )
    return "inventory" if inventory else "ambiguous"


def selector_summary(selector: Any) -> str:
    if selector in (None, {}):
        return "tüm namespace'ler"
    if not isinstance(selector, dict):
        return "selector yapılandırılmış"
    parts: list[str] = []
    labels = selector.get("matchLabels")
    if isinstance(labels, dict):
        parts.extend(
            f"{key}={value}" for key, value in list(labels.items())[:5]
            if isinstance(key, str) and isinstance(value, str)
        )
    expressions = selector.get("matchExpressions")
    if isinstance(expressions, list):
        for expression in expressions[:3]:
            if not isinstance(expression, dict):
                continue
            key, operator, values = (
                expression.get("key"), expression.get("operator"),
                expression.get("values"),
            )
            if not isinstance(key, str) or not isinstance(operator, str):
                continue
            rendered_values = ",".join(
                str(value) for value in values[:5]
            ) if isinstance(values, list) else ""
            parts.append(
                f"{key} {operator} ({rendered_values})"
                if rendered_values else f"{key} {operator}"
            )
    return ", ".join(parts) if parts else "selector yapılandırılmış"


def egressip_inventory_record(item: dict[str, Any]) -> dict[str, Any] | None:
    # The official OpenShift MCP server's default table output exposes rows as
    # structuredContent.items with Kubernetes column names. A Name-only row is
    # still a valid result for this fixed-kind query; unavailable detail stays
    # explicit instead of triggering speculative resources_get calls.
    if not {"apiVersion", "kind", "metadata", "spec"} & set(item):
        table_name = item.get("Name")
        if isinstance(table_name, str) and table_name:
            return {
                "name": table_name,
                "configured_ips": None,
                "assignments": None,
                "namespace_selector": None,
                "pod_selector": None,
                "details_authoritative": False,
            }
        return None
    if item.get("apiVersion") != "k8s.ovn.org/v1" or item.get("kind") != "EgressIP":
        return None
    metadata, spec = item.get("metadata"), item.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        return None
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        return None
    assignments: list[dict[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    status = item.get("status")
    status_items = status.get("items") if isinstance(status, dict) else None
    if isinstance(status_items, list):
        for assignment in status_items[:20]:
            if not isinstance(assignment, dict):
                continue
            address = assignment.get("egressIP")
            node = assignment.get("node")
            if not isinstance(address, str):
                continue
            safe_node = node if isinstance(node, str) else None
            identity = (address, safe_node)
            if identity in seen:
                continue
            seen.add(identity)
            assignments.append({"ip": address, "node": safe_node})
    return {
        "name": name,
        "configured_ips": list(dict.fromkeys(
            address for address in spec.get("egressIPs", [])[:20]
            if isinstance(address, str) and address
        )) if isinstance(spec.get("egressIPs"), list) else [],
        "assignments": assignments,
        "namespace_selector": selector_summary(spec.get("namespaceSelector")),
        "pod_selector": (
            None if "podSelector" not in spec
            else spec.get("podSelector") not in (None, {})
        ),
        "details_authoritative": True,
    }


def selector_matches(selector: Any, labels: dict[str, str]) -> bool:
    if selector in (None, {}):
        return True
    if not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        return False
    if any(labels.get(str(key)) != value for key, value in match_labels.items()):
        return False
    for expression in expressions:
        if not isinstance(expression, dict):
            return False
        key, operator, values = (
            expression.get("key"), expression.get("operator"),
            expression.get("values", []),
        )
        if not isinstance(key, str) or not isinstance(values, list):
            return False
        present = key in labels
        if operator == "In" and (not present or labels[key] not in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            return False
    return True


def selector_is_valid(selector: Any) -> bool:
    if selector in (None, {}):
        return True
    if not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        return False
    for expression in expressions:
        if not isinstance(expression, dict):
            return False
        operator = expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(expression.get("key"), str) or operator not in {
            "In", "NotIn", "Exists", "DoesNotExist",
        } or not isinstance(values, list):
            return False
        if operator in {"In", "NotIn"} and not values:
            return False
        if operator in {"Exists", "DoesNotExist"} and values:
            return False
    return True


def egressip_has_full_detail(item: dict[str, Any]) -> bool:
    spec = item.get("spec")
    return (
        item.get("apiVersion") == "k8s.ovn.org/v1"
        and item.get("kind") == "EgressIP"
        and isinstance(item.get("metadata"), dict)
        and isinstance(spec, dict)
    )


def namespace_labels(items: list[dict[str, Any]], namespace: str) -> dict[str, str] | None:
    for item in items:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("name") != namespace:
            continue
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            return None
        return {
            str(key): str(value) for key, value in labels.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    return None


def matching_egressips(
    items: list[dict[str, Any]], labels: dict[str, str]
) -> list[dict[str, Any]]:
    matches = []
    for item in items:
        if item.get("apiVersion") != "k8s.ovn.org/v1" or item.get("kind") != "EgressIP":
            continue
        spec = item.get("spec")
        metadata = item.get("metadata")
        if not isinstance(spec, dict) or not isinstance(metadata, dict):
            continue
        if not selector_matches(spec.get("namespaceSelector"), labels):
            continue
        assignments = []
        seen_assignments: set[tuple[str, str | None]] = set()
        status = item.get("status")
        status_items = status.get("items") if isinstance(status, dict) else None
        if isinstance(status_items, list):
            for assignment in status_items:
                if not isinstance(assignment, dict) or not isinstance(assignment.get("egressIP"), str):
                    continue
                node = assignment.get("node") if isinstance(assignment.get("node"), str) else None
                identity = (assignment["egressIP"], node)
                if identity not in seen_assignments:
                    seen_assignments.add(identity)
                    assignments.append({"ip": assignment["egressIP"], "node": node})
        matches.append({
            "name": str(metadata.get("name") or "EgressIP"),
            "configured_ips": list(dict.fromkeys(
                address for address in spec.get("egressIPs", [])[:20]
                if isinstance(address, str) and address
            )) if isinstance(spec.get("egressIPs"), list) else [],
            "assignments": assignments,
            "namespace_selector": selector_summary(spec.get("namespaceSelector")),
            "pod_selector": spec.get("podSelector") not in (None, {}),
        })
    return matches


def evaluate_egressips(
    items: list[dict[str, Any]], labels: dict[str, str]
) -> tuple[list[dict[str, Any]], bool]:
    for item in items:
        if item.get("apiVersion") != "k8s.ovn.org/v1" or item.get("kind") != "EgressIP":
            continue
        spec = item.get("spec")
        if not isinstance(spec, dict) or not selector_is_valid(spec.get("namespaceSelector")):
            return [], False
    return matching_egressips(items, labels), True
