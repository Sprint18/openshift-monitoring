from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal


NormalizationStatus = Literal[
    "success", "empty", "tool_error", "malformed", "unsupported",
]
_KNOWN_WRAPPERS = ("structuredContent", "result", "resource", "object", "data")
_KNOWN_KUBERNETES_FIELDS = (
    "apiVersion", "kind", "metadata", "name", "labels", "spec", "status",
)


@dataclass(frozen=True)
class NormalizedMCPResult:
    status: NormalizationStatus
    source: str
    objects: tuple[dict[str, Any], ...] = ()
    table_rows: tuple[dict[str, Any], ...] = ()
    raw_item_count: int = 0
    representation: str = "unknown"
    completeness: str = "unknown"
    authoritative_fields: tuple[str, ...] = ()

    @property
    def items(self) -> list[dict[str, Any]] | None:
        if self.status not in {"success", "empty"}:
            return None
        return list(self.objects or self.table_rows)


def _decoded_json(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("data:"):
        candidate = candidate.removeprefix("data:").strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    if not candidate.startswith(("{", "[")):
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _text_structure(text: str) -> tuple[str, ...]:
    """Describe syntax without exposing any text values."""
    lines = text.splitlines()
    nonempty = [line for line in lines if line.strip()]
    mapping_lines = [
        line for line in nonempty
        if re.match(r"^\s*[A-Za-z][A-Za-z0-9_.-]*\s*:", line)
    ]
    sequence_lines = [line for line in nonempty if re.match(r"^\s*-\s+", line)]
    indented_lines = [
        line for line in nonempty if line[:1].isspace() and not line.startswith("\t")
    ]
    field_names = {
        match.group(1)
        for line in mapping_lines
        if (match := re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*)\s*:", line))
    }
    if mapping_lines and indented_lines:
        syntax = "indented_key_value"
    elif mapping_lines:
        syntax = "flat_key_value"
    else:
        syntax = "other"
    return (
        f"line_count={len(lines)}",
        f"nonempty_lines={len(nonempty)}",
        f"mapping_lines={len(mapping_lines)}",
        f"sequence_lines={len(sequence_lines)}",
        f"indented_lines={len(indented_lines)}",
        f"tabs={str(any(line.startswith(chr(9)) for line in nonempty)).lower()}",
        f"document_marker={str(any(line.strip() in {'---', '...'} for line in nonempty)).lower()}",
        "known_fields=" + ",".join(
            field for field in _KNOWN_KUBERNETES_FIELDS if field in field_names
        ),
        f"text_syntax={syntax}",
    )


def _table_rows(text: str) -> list[dict[str, Any]] | None:
    """Parse the kubectl TablePrinter contract, not arbitrary whitespace text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header_index = next((
        index for index, line in enumerate(lines)
        if {"APIVERSION", "KIND", "NAME"}.issubset(line.upper().split())
    ), None)
    if header_index is None:
        return None
    headers = lines[header_index].upper().split()
    required_indexes = {name: headers.index(name) for name in (
        "APIVERSION", "KIND", "NAME",
    )}
    namespace_index = headers.index("NAMESPACE") if "NAMESPACE" in headers else None
    minimum_columns = max(required_indexes.values()) + 1
    rows: list[dict[str, Any]] = []
    for line in lines[header_index + 1:]:
        columns = line.split()
        if len(columns) < minimum_columns:
            continue
        row = {
            "ApiVersion": columns[required_indexes["APIVERSION"]],
            "Kind": columns[required_indexes["KIND"]],
            "Name": columns[required_indexes["NAME"]],
        }
        if namespace_index is not None and namespace_index < len(columns):
            row["Namespace"] = columns[namespace_index]
        rows.append(row)
    return rows


def _bare_resource_rows(text: str) -> list[dict[str, Any]] | None:
    """Support the legacy API-version/kind/name rows emitted without a header."""
    rows: list[dict[str, Any]] = []
    for line in (line.strip() for line in text.splitlines() if line.strip()):
        columns = line.split()
        if len(columns) < 3 or not re.fullmatch(
            r"(?:[a-z0-9.-]+/)?v[0-9][a-z0-9]*", columns[0], re.IGNORECASE
        ) or not re.fullmatch(r"[A-Z][A-Za-z0-9]+", columns[1]):
            return None
        rows.append({
            "ApiVersion": columns[0], "Kind": columns[1], "Name": columns[2],
        })
    return rows or None


def _table_collection(items: list[dict[str, Any]], source: str) -> NormalizedMCPResult | None:
    if not items or not all(
        isinstance(item.get("Name"), str)
        and not isinstance(item.get("metadata"), dict)
        for item in items
    ):
        return None
    return NormalizedMCPResult(
        "success", source, table_rows=tuple(items), raw_item_count=len(items),
        representation="kubectl_table", completeness="names_only",
        authoritative_fields=("apiVersion", "kind", "metadata.name"),
    )


def _canonical_payload(value: Any, source: str) -> NormalizedMCPResult | None:
    if isinstance(value, list):
        if not all(isinstance(item, dict) for item in value):
            return NormalizedMCPResult("malformed", source)
        if table := _table_collection(value, source):
            return table
        status: NormalizationStatus = "success" if value else "empty"
        return NormalizedMCPResult(
            status, source, tuple(value), raw_item_count=len(value),
            representation="kubernetes_objects", completeness="full",
            authoritative_fields=("*",),
        )
    if not isinstance(value, dict):
        return None
    items = value.get("items")
    if isinstance(items, list):
        if not all(isinstance(item, dict) for item in items):
            return NormalizedMCPResult("malformed", source)
        if table := _table_collection(items, source):
            return table
        status = "success" if items else "empty"
        return NormalizedMCPResult(
            status, source, tuple(items), raw_item_count=len(items),
            representation="kubernetes_list", completeness="full",
            authoritative_fields=("*",),
        )
    if isinstance(value.get("metadata"), dict) or isinstance(value.get("spec"), dict):
        return NormalizedMCPResult(
            "success", source, (value,), raw_item_count=1,
            representation="kubernetes_object", completeness="full",
            authoritative_fields=("*",),
        )
    return None


def _known_payloads(result: dict[str, Any]) -> list[tuple[Any, str]]:
    payloads: list[tuple[Any, str]] = [(result, "direct")]
    queue: list[tuple[dict[str, Any], str, int]] = [(result, "direct", 0)]
    seen = {id(result)}
    while queue:
        container, source, depth = queue.pop(0)
        if depth >= 3:
            continue
        for key in _KNOWN_WRAPPERS:
            nested = container.get(key)
            if not isinstance(nested, (dict, list)) or id(nested) in seen:
                continue
            seen.add(id(nested))
            nested_source = f"{source}.{key}"
            payloads.append((nested, nested_source))
            if isinstance(nested, dict):
                queue.append((nested, nested_source, depth + 1))
    return payloads


def normalize_mcp_result(result: Any) -> NormalizedMCPResult:
    if not isinstance(result, dict):
        return NormalizedMCPResult("malformed", "root")
    if result.get("isError") is True:
        return NormalizedMCPResult("tool_error", "isError")
    for value, source in _known_payloads(result):
        if normalized := _canonical_payload(value, source):
            return normalized
    content_blocks = [
        block for payload, _source in _known_payloads(result)
        if isinstance(payload, dict) and isinstance(payload.get("content"), list)
        for block in payload["content"]
    ]
    if not content_blocks:
        return NormalizedMCPResult("unsupported", "root")
    saw_text = False
    saw_malformed = False
    for block in content_blocks[:20]:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            saw_malformed = True
            continue
        saw_text = True
        decoded = _decoded_json(text)
        if text.lstrip().startswith(("{", "[")) and decoded is None:
            saw_malformed = True
        if decoded is not None:
            if normalized := _canonical_payload(decoded, "content_text_json"):
                return normalized
        rows = _table_rows(text)
        if rows is None:
            rows = _bare_resource_rows(text)
        if rows is not None:
            status: NormalizationStatus = "success" if rows else "empty"
            return NormalizedMCPResult(
                status, "content_text_table", table_rows=tuple(rows),
                raw_item_count=len(rows), representation="kubectl_table",
                completeness="names_only",
                authoritative_fields=("apiVersion", "kind", "metadata.name"),
            )
    return NormalizedMCPResult(
        "malformed" if saw_malformed else "unsupported",
        "content_text" if saw_text else "content",
    )


def mcp_result_shape(result: Any) -> str:
    """Bounded structural diagnostics; never include content or resource values."""
    if not isinstance(result, dict):
        return f"root_type={type(result).__name__}"
    parts = [
        "root_type=dict",
        "root_keys=" + ",".join(sorted(str(key) for key in result)[:12]),
        f"structured_type={type(result.get('structuredContent')).__name__}",
    ]
    content = result.get("content")
    parts.append(f"content_count={len(content) if isinstance(content, list) else 0}")
    if isinstance(content, list) and content:
        block = content[0]
        parts.append(f"content0_type={type(block).__name__}")
        if isinstance(block, dict):
            parts.append("content0_keys=" + ",".join(sorted(str(key) for key in block)[:8]))
            parts.append(f"content0_kind={block.get('type') if isinstance(block.get('type'), str) else 'none'}")
            text = block.get("text")
            parts.append(f"text_present={str(isinstance(text, str)).lower()}")
            if isinstance(text, str):
                stripped = text.lstrip()
                parsed_json = _decoded_json(text)
                parsed = parsed_json
                parsed_mapping = parsed if isinstance(parsed, dict) else {}
                parts.extend((
                    f"text_length={len(text)}",
                    f"first_char={stripped[:1] if stripped else 'empty'}",
                    f"last_char={stripped[-1:] if stripped else 'empty'}",
                    f"json={str(parsed_json is not None).lower()}",
                    f"parsed_type={type(parsed).__name__}",
                    "parsed_keys=" + (
                        ",".join(sorted(str(key) for key in parsed)[:10])
                        if isinstance(parsed, dict) else ""
                    ),
                    f"has_items={str(isinstance(parsed_mapping.get('items'), list)).lower()}",
                    f"has_kind={str('kind' in parsed_mapping).lower()}",
                    f"has_api_version={str('apiVersion' in parsed_mapping).lower()}",
                    "known_wrappers=" + ",".join(
                        key for key in _KNOWN_WRAPPERS if key in parsed_mapping
                    ),
                    f"table={str(_table_rows(text) is not None).lower()}",
                    f"plain={str(parsed is None and _table_rows(text) is None).lower()}",
                    *_text_structure(text),
                ))
    normalized = normalize_mcp_result(result)
    parts.extend((
        f"source={normalized.source}", f"status={normalized.status}",
        f"representation={normalized.representation}",
        f"completeness={normalized.completeness}",
        f"raw_item_count={normalized.raw_item_count}",
        f"normalized_count={len(normalized.objects or normalized.table_rows)}",
    ))
    return " ".join(parts)


def resource_items(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    return normalize_mcp_result(result).items


def resource_object(result: dict[str, Any]) -> dict[str, Any] | None:
    normalized = normalize_mcp_result(result)
    return normalized.objects[0] if normalized.objects else None


def resource_names(result: dict[str, Any], kind: str) -> list[str] | None:
    normalized = normalize_mcp_result(result)
    candidates = normalized.objects or normalized.table_rows
    names: list[str] = []
    for item in candidates:
        metadata = item.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else item.get("Name")
        item_kind = item.get("kind", item.get("Kind"))
        if isinstance(name, str) and name and (
            not isinstance(item_kind, str) or item_kind.casefold() == kind.casefold()
        ):
            names.append(name)
    return list(dict.fromkeys(names)) if names or normalized.status == "empty" else None
