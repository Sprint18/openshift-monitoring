from __future__ import annotations

import json
import re
from typing import Any


_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"


def egressip_namespace(message: str) -> str | None:
    normalized = " ".join(message.casefold().replace("’", "'").split())
    if not re.search(r"\begress\s*ip\b", normalized):
        return None
    patterns = (
        rf"\b({_DNS_LABEL})\s+namespace\s+egress\s*ip\b",
        rf"\b({_DNS_LABEL})(?:'?(?:ye|ya|e|a))?\s+(?:ait\s+)?egress\s*ip\b",
        rf"\b(?:namespace|proje)\s+({_DNS_LABEL})(?:'?(?:nin|nın|nun|nün))?.*?\begress\s*ip\b",
        rf"\begress\s*ip\b.*?\b(?:namespace|proje)\s+({_DNS_LABEL})\b",
    )
    excluded = {"hangi", "mevcut", "atanmış", "atanmis", "kullandığı", "kullandigi"}
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match and match.group(1) not in excluded:
            return match.group(1)
    return None


def is_egressip_intent(message: str) -> bool:
    return re.search(r"\begress\s*ip\b", message.casefold()) is not None


def resource_items(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    candidates: list[Any] = [result, result.get("structuredContent")]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                candidates.append(json.loads(item["text"]))
            except json.JSONDecodeError:
                continue
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("items"), list):
            return [item for item in candidate["items"] if isinstance(item, dict)]
    return None


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
        status = item.get("status")
        status_items = status.get("items") if isinstance(status, dict) else None
        if isinstance(status_items, list):
            for assignment in status_items:
                if not isinstance(assignment, dict) or not isinstance(assignment.get("egressIP"), str):
                    continue
                assignments.append({
                    "ip": assignment["egressIP"],
                    "node": assignment.get("node") if isinstance(assignment.get("node"), str) else None,
                })
        matches.append({
            "name": str(metadata.get("name") or "EgressIP"),
            "assignments": assignments,
            "pod_selector": spec.get("podSelector") not in (None, {}),
        })
    return matches
