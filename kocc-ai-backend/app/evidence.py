from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


EvidenceSource = Literal["mcp", "kubernetes_api"]
EvidenceCompleteness = Literal["complete", "partial", "unavailable"]

LEGACY_PUBLIC_FACT_KEYS = frozenset({
    "resource_count",
    "degraded_true_count",
    "available_false_count",
    "progressing_true_count",
})


@dataclass(frozen=True)
class EvidenceResource:
    api_version: str | None = None
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class EvidenceEnvelope:
    evidence_id: str
    cluster_id: str
    source: EvidenceSource
    operation: str
    resource: EvidenceResource
    observed_at: str
    completeness: EvidenceCompleteness
    facts: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        cluster_id: str,
        operation: str,
        resource: EvidenceResource,
        completeness: EvidenceCompleteness,
        facts: dict[str, Any] | None = None,
        warnings: tuple[str, ...] = (),
        provenance: dict[str, str] | None = None,
        source: EvidenceSource = "mcp",
    ) -> "EvidenceEnvelope":
        return cls(
            evidence_id=str(uuid4()),
            cluster_id=cluster_id,
            source=source,
            operation=operation,
            resource=resource,
            observed_at=datetime.now(timezone.utc).isoformat(),
            completeness=completeness,
            facts=dict(facts or {}),
            warnings=warnings,
            provenance=dict(provenance or {}),
        )

    def legacy_metadata(self) -> dict[str, Any]:
        """Project canonical evidence onto the backward-compatible API shape."""
        metadata: dict[str, Any] = {
            "tool": self.provenance.get("tool", self.operation),
            "status": "success" if self.completeness != "unavailable" else "error",
        }
        public_facts = {
            key: value for key, value in self.facts.items()
            if key in LEGACY_PUBLIC_FACT_KEYS
            and isinstance(value, int) and not isinstance(value, bool)
        }
        if public_facts:
            metadata["facts"] = public_facts
        return metadata
