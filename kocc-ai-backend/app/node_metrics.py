from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NodeMetric:
    node_name: str
    cpu_raw: str
    cpu_millicores: float | None
    cpu_percent: float | None
    memory_raw: str
    memory_percent: float | None


@dataclass(frozen=True)
class NodeMetricsFacts:
    cluster_id: str | None
    nodes: tuple[NodeMetric, ...]


def _cpu_millicores(value: str) -> float | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(n|u|m)?", value.strip())
    if not match:
        return None
    amount = float(match.group(1))
    return amount / 1_000_000 if match.group(2) == "n" else (
        amount / 1_000 if match.group(2) == "u" else
        amount if match.group(2) == "m" else amount * 1000
    )


def _percent(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)%", value.strip())
        if match:
            return float(match.group(1))
    return None


def _metric(item: Any) -> NodeMetric | None:
    if not isinstance(item, dict):
        return None
    metadata = item.get("metadata")
    usage = item.get("usage")
    name = item.get("name")
    if not isinstance(name, str) and isinstance(metadata, dict):
        name = metadata.get("name")
    cpu = item.get("cpu")
    memory = item.get("memory")
    if isinstance(usage, dict):
        cpu = usage.get("cpu", cpu)
        memory = usage.get("memory", memory)
    if not all(isinstance(value, str) and value.strip() for value in (name, cpu, memory)):
        return None
    return NodeMetric(
        name.strip(), cpu.strip(), _cpu_millicores(cpu),
        _percent(item.get("cpuPercent", item.get("cpu_percent"))),
        memory.strip(),
        _percent(item.get("memoryPercent", item.get("memory_percent"))),
    )


def _objects(result: dict[str, Any]) -> list[Any]:
    values: list[Any] = [result, result.get("structuredContent")]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                values.append(json.loads(item["text"]))
            except json.JSONDecodeError:
                continue
    return values


def parse_node_metrics(
    result: dict[str, Any], cluster_id: str | None = None
) -> NodeMetricsFacts | None:
    for payload in _objects(result):
        if not isinstance(payload, dict):
            continue
        candidates = payload.get("nodes", payload.get("items"))
        if not isinstance(candidates, list):
            continue
        nodes = tuple(metric for item in candidates if (metric := _metric(item)))
        if nodes:
            return NodeMetricsFacts(cluster_id, nodes)

    lines: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        lines = [
            line for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
            for line in item["text"].splitlines()
        ]
    parsed = []
    for line in lines:
        columns = line.strip().strip("|").replace("|", " ").split()
        if len(columns) < 3 or columns[0].casefold() in {"name", "node"}:
            continue
        cpu_millicores = _cpu_millicores(columns[1])
        if cpu_millicores is None:
            continue
        has_percentages = len(columns) >= 5 and _percent(columns[2]) is not None
        cpu_percent = _percent(columns[2]) if has_percentages else None
        memory_index = 3 if has_percentages else 2
        memory_percent = _percent(columns[4]) if has_percentages else None
        parsed.append(NodeMetric(
            columns[0], columns[1], cpu_millicores, cpu_percent,
            columns[memory_index], memory_percent,
        ))
    return NodeMetricsFacts(cluster_id, tuple(parsed)) if parsed else None


def render_node_metrics(facts: NodeMetricsFacts) -> str:
    rows = [
        "## Node CPU ve Memory Kullanımı",
        "",
        "| Node | CPU | CPU % | Memory | Memory % |",
        "|---|---:|---:|---:|---:|",
    ]
    for node in facts.nodes:
        cpu_percent = f"{node.cpu_percent:g}%" if node.cpu_percent is not None else "N/A"
        memory_percent = (
            f"{node.memory_percent:g}%" if node.memory_percent is not None else "N/A"
        )
        rows.append(
            f"| {node.node_name} | {node.cpu_raw} | {cpu_percent} | "
            f"{node.memory_raw} | {memory_percent} |"
        )
    return "\n".join(rows)
