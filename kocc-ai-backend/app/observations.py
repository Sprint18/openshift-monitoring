from __future__ import annotations

import json
from typing import Any


def _json_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        objects.append(value)
        structured = value.get("structuredContent")
        if isinstance(structured, dict):
            objects.append(structured)
        content = value.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                try:
                    parsed = json.loads(item["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    objects.append(parsed)
    return objects


def _resource_kind(arguments: dict[str, Any], payload: dict[str, Any]) -> str | None:
    for key in ("kind", "resource", "resourceType"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value.removesuffix("List")
    kind = payload.get("kind")
    if isinstance(kind, str) and kind:
        return kind.removesuffix("List")
    items = payload.get("items")
    if isinstance(items, list):
        kinds = {
            item.get("kind") for item in items
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        }
        if len(kinds) == 1:
            return kinds.pop()
    return None


def _condition_true(item: dict[str, Any], condition_type: str, expected: str) -> bool:
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("type") == condition_type
        and str(condition.get("status", "")).lower() == expected.lower()
        for condition in conditions
    )


def deterministic_observation(
    tool_name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Extract safe authoritative facts before the MCP result is truncated."""
    if tool_name not in {"resources_list", "pods_list", "namespaces_list"}:
        return {}
    payload = next(
        (
            candidate for candidate in _json_objects(result)
            if isinstance(candidate.get("items"), list)
        ),
        None,
    )
    if payload is None:
        return {}
    items = payload["items"]
    facts: dict[str, Any] = {"resource_count": len(items)}
    kind = _resource_kind(arguments, payload)
    if kind:
        facts["kind"] = kind
    if kind and kind.lower() in {"clusteroperator", "clusteroperators"}:
        facts.update({
            "kind": "ClusterOperator",
            "degraded_true_count": sum(
                _condition_true(item, "Degraded", "True")
                for item in items if isinstance(item, dict)
            ),
            "available_false_count": sum(
                _condition_true(item, "Available", "False")
                for item in items if isinstance(item, dict)
            ),
            "progressing_true_count": sum(
                _condition_true(item, "Progressing", "True")
                for item in items if isinstance(item, dict)
            ),
        })
    return facts
