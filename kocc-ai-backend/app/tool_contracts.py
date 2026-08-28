from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceIdentity:
    api_version: str
    kind: str


KNOWN_RESOURCE_IDENTITIES = {
    "clusteroperator": ResourceIdentity(
        api_version="config.openshift.io/v1", kind="ClusterOperator"
    ),
}


def canonical_resource_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    schema: dict[str, Any],
    identity: ResourceIdentity | None = None,
) -> dict[str, Any]:
    """Normalize only known resource identities using names exposed by the schema."""
    if tool_name not in {"resources_list", "resources_get"}:
        return dict(arguments)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return dict(arguments)
    candidate = identity
    kind = arguments.get("kind")
    if candidate is None and isinstance(kind, str):
        candidate = KNOWN_RESOURCE_IDENTITIES.get(
            kind.replace(" ", "").lower().removesuffix("s")
        )
    if candidate is None:
        return dict(arguments)
    normalized = dict(arguments)
    if "apiVersion" in properties:
        normalized["apiVersion"] = candidate.api_version
    if "kind" in properties:
        normalized["kind"] = candidate.kind
    return normalized


def validate_tool_arguments(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    """Validate the JSON Schema subset emitted by the MCP tools/list contract."""
    if schema.get("type") not in (None, "object"):
        return "Tool schema does not describe an object."
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return "Tool schema properties are invalid."
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return "Tool arguments contain unknown fields."
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        return "Tool schema required fields are invalid."
    if any(key not in arguments for key in required):
        return "Tool arguments are missing required fields."
    for key, value in arguments.items():
        definition = properties.get(key)
        if not isinstance(definition, dict):
            return "Tool argument schema is invalid."
        expected = definition.get("type")
        type_matches = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }
        if expected in type_matches and not type_matches[expected]:
            return "Tool argument types do not match the schema."
        enum = definition.get("enum")
        if isinstance(enum, list) and value not in enum:
            return "Tool argument is outside the allowed values."
    return None
