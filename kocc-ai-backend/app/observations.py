from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClusterOperatorFacts:
    cluster_id: str | None
    total: int
    available_false_count: int
    progressing_true_count: int
    degraded_true_count: int
    unavailable_names: tuple[str, ...] = ()
    progressing_names: tuple[str, ...] = ()
    degraded_names: tuple[str, ...] = ()

    def public_facts(self) -> dict[str, int]:
        return {
            "resource_count": self.total,
            "available_false_count": self.available_false_count,
            "progressing_true_count": self.progressing_true_count,
            "degraded_true_count": self.degraded_true_count,
        }


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


def _structured_operator_record(
    item: Any,
) -> tuple[str, bool, bool, bool] | None:
    if not isinstance(item, dict):
        return None
    if item.get("apiVersion") != "config.openshift.io/v1":
        return None
    if item.get("kind") != "ClusterOperator":
        return None
    metadata = item.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    states: dict[str, bool] = {}
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        condition_type = condition.get("type")
        condition_status = condition.get("status")
        if condition_type in {"Available", "Progressing", "Degraded"}:
            if condition_status not in {"True", "False"}:
                return None
            states[condition_type] = condition_status == "True"
    if set(states) != {"Available", "Progressing", "Degraded"}:
        return None
    return name.strip(), states["Available"], states["Progressing"], states["Degraded"]


def _text_fragments(value: Any) -> list[str]:
    fragments: list[str] = []
    if isinstance(value, str):
        fragments.append(value)
        candidate = value.strip()
        if candidate.startswith("data:"):
            candidate = candidate.removeprefix("data:").strip()
        if candidate.startswith(("{", "[")):
            try:
                _collect_text_fragments(json.loads(candidate), fragments)
            except json.JSONDecodeError:
                pass
    else:
        _collect_text_fragments(value, fragments)
    return fragments


def _collect_text_fragments(value: Any, fragments: list[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _collect_text_fragments(nested, fragments)
    elif isinstance(value, list):
        for nested in value:
            _collect_text_fragments(nested, fragments)
    elif isinstance(value, str):
        fragments.append(value)
        candidate = value.strip()
        if candidate.startswith("data:"):
            candidate = candidate.removeprefix("data:").strip()
        if candidate.startswith(("{", "[")):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                return
            _collect_text_fragments(parsed, fragments)


_TABLE_OPERATOR = re.compile(
    r"^config\.openshift\.io/v1\s+ClusterOperator\s+(\S+)\s+"
    r".*?\s+(True|False)\s+(True|False)\s+(True|False)(?:\s|$)",
    flags=re.IGNORECASE,
)


def _table_operator_records(result: dict[str, Any]) -> list[tuple[str, bool, bool, bool]]:
    records: dict[str, tuple[str, bool, bool, bool]] = {}
    fragments = _text_fragments(result)
    index = 0
    while index < len(fragments):
        fragment = fragments[index]
        index += 1
        for raw_line in fragment.splitlines():
            line = raw_line.strip().strip("`|").replace("|", " ")
            if line.startswith("data:"):
                line = line.removeprefix("data:").strip()
                if line.startswith(("{", "[")):
                    try:
                        fragments.extend(_text_fragments(json.loads(line)))
                    except json.JSONDecodeError:
                        pass
            match = _TABLE_OPERATOR.match(line)
            if not match:
                continue
            name, available, progressing, degraded = match.groups()
            records[name] = (
                name,
                available.lower() == "true",
                progressing.lower() == "true",
                degraded.lower() == "true",
            )
    return list(records.values())


def cluster_operator_facts(
    result: dict[str, Any], cluster_id: str | None = None
) -> ClusterOperatorFacts | None:
    records: list[tuple[str, bool, bool, bool]] = []
    for payload in _json_objects(result):
        items = payload.get("items")
        if not isinstance(items, list):
            continue
        records = [
            record for item in items
            if (record := _structured_operator_record(item)) is not None
        ]
        if records:
            break
    if not records:
        records = _table_operator_records(result)
    if not records:
        return None
    unavailable = tuple(name for name, available, _p, _d in records if not available)
    progressing = tuple(name for name, _a, value, _d in records if value)
    degraded = tuple(name for name, _a, _p, value in records if value)
    return ClusterOperatorFacts(
        cluster_id=cluster_id,
        total=len(records),
        available_false_count=len(unavailable),
        progressing_true_count=len(progressing),
        degraded_true_count=len(degraded),
        unavailable_names=unavailable,
        progressing_names=progressing,
        degraded_names=degraded,
    )


def deterministic_observation(
    tool_name: str, arguments: dict[str, Any], result: dict[str, Any],
    cluster_id: str | None = None,
) -> dict[str, Any]:
    """Extract safe authoritative facts before the MCP result is truncated."""
    if tool_name not in {
        "resources_list", "pods_list", "pods_list_in_namespace", "namespaces_list",
    }:
        return {}
    requested_kind = next((
        arguments.get(key) for key in ("kind", "resource", "resourceType")
        if isinstance(arguments.get(key), str)
    ), None)
    if requested_kind and requested_kind.removesuffix("List").lower() in {
        "clusteroperator", "clusteroperators",
    }:
        facts = cluster_operator_facts(result, cluster_id)
        return ({"kind": "ClusterOperator", **facts.public_facts()} if facts else {})
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
    if tool_name in {"pods_list", "pods_list_in_namespace"}:
        ready_count = 0
        total_restarts = 0
        max_restart_count = 0
        problematic_names: list[str] = []
        phase_counts: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            status = item.get("status")
            if not isinstance(status, dict):
                status = {}
            phase = str(status.get("phase") or "Unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            containers = status.get("containerStatuses")
            container_rows = containers if isinstance(containers, list) else []
            fully_ready = bool(container_rows) and all(
                isinstance(container, dict) and container.get("ready") is True
                for container in container_rows
            )
            if fully_ready:
                ready_count += 1
            else:
                name = metadata.get("name") if isinstance(metadata, dict) else None
                if isinstance(name, str) and name and len(problematic_names) < 10:
                    problematic_names.append(name)
            pod_restarts = sum(
                value for container in container_rows
                if isinstance(container, dict)
                and isinstance((value := container.get("restartCount")), int)
                and not isinstance(value, bool) and value >= 0
            )
            total_restarts += pod_restarts
            max_restart_count = max(max_restart_count, pod_restarts)
        facts.update({
            "pod_count": len(items),
            "ready_count": ready_count,
            "non_ready_count": max(0, len(items) - ready_count),
            "total_restarts": total_restarts,
            "max_restart_count": max_restart_count,
            "problematic_pod_names": problematic_names,
            "phase_counts": phase_counts,
        })
    return facts
