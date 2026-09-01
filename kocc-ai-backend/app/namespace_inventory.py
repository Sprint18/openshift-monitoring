from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from app.agent import AgentResult
from app.evidence import EvidenceEnvelope, EvidenceResource
from app.k8s_client import KubernetesAPIAdapter, KubernetesListResult


NamespaceQueryMode = Literal["prefix", "contains", "exact"]
RESERVED_EXACT_VALUES = frozenset({
    "durum", "health", "liste", "listesi", "status", "var",
})


@dataclass(frozen=True)
class NamespaceQuery:
    mode: NamespaceQueryMode
    value: str
    list_names: bool = False


def _normalize_text(value: str) -> str:
    folded = unicodedata.normalize(
        "NFKD", value.casefold().replace("’", "'").translate(str.maketrans({"ı": "i"}))
    )
    return "".join(
        character for character in folded if not unicodedata.combining(character)
    )


def parse_namespace_query(message: str) -> NamespaceQuery | None:
    normalized = " ".join(_normalize_text(message).split())
    resource = r"(?:namespace|namespaces|ns|proje|project)[a-z']*"
    name = r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"

    prefix = re.search(
        rf"\b(?P<value>{name})\s+ile\s+baslayan\b.*?\b{resource}\b",
        normalized,
    )
    if prefix:
        list_names = any(term in normalized for term in ("listele", "listesi", "namespace'leri", "namespaceleri"))
        return NamespaceQuery("prefix", prefix.group("value"), list_names)

    contains = re.search(
        rf"\b(?P<value>{name})\s+iceren\b.*?\b{resource}\b", normalized
    )
    if contains:
        return NamespaceQuery("contains", contains.group("value"), True)

    exact_patterns = (
        rf"\b(?P<value>{name})\s+{resource}\b",
        rf"\b{resource}\s+(?P<value>{name})\b",
        rf"^(?P<value>{name})\s+var\s+mi[?]?$",
    )
    for pattern in exact_patterns:
        match = re.search(pattern, normalized)
        if match and match.group("value") not in RESERVED_EXACT_VALUES:
            return NamespaceQuery("exact", match.group("value"))
    return None


def _namespace_name(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) and name else None


def _namespace_phase(item: dict[str, Any]) -> str | None:
    status = item.get("status")
    phase = status.get("phase") if isinstance(status, dict) else None
    return phase if isinstance(phase, str) and phase else None


def namespace_suggestions(
    requested: str, names: list[str], *, complete: bool
) -> list[str]:
    if not complete:
        return []
    return difflib.get_close_matches(requested, names, n=3, cutoff=0.76)


def _envelope(
    cluster_id: str,
    result: KubernetesListResult,
    facts: dict[str, Any],
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        cluster_id=cluster_id,
        source="kubernetes_api",
        operation="list",
        resource=EvidenceResource(api_version="v1", kind="Namespace"),
        completeness=result.completeness,
        facts=facts,
        warnings=result.warnings,
        provenance={"tool": "k8s_list", "transport": "in_cluster"},
    )


def execute_namespace_query(
    adapter: KubernetesAPIAdapter,
    cluster_id: str,
    query: NamespaceQuery,
) -> AgentResult:
    result = adapter.k8s_list(api_version="v1", kind="Namespace")
    names_by_folded = {
        name.casefold(): (name, item)
        for item in result.items
        if (name := _namespace_name(item)) is not None
    }
    names = sorted((value[0] for value in names_by_folded.values()), key=str.casefold)
    base_facts: dict[str, Any] = {"total_items": len(names)}

    if result.completeness != "complete":
        envelope = _envelope(cluster_id, result, base_facts)
        return AgentResult(
            "Namespace listesi tamamlanamadığı için kesin sonuç veremiyorum.",
            [{"name": "k8s_list", "status": "error"}], [envelope], 0,
        )

    if query.mode in {"prefix", "contains"}:
        folded = query.value.casefold()
        matched = [
            name for name in names
            if (name.casefold().startswith(folded) if query.mode == "prefix"
                else folded in name.casefold())
        ]
        relation = "ile başlayan" if query.mode == "prefix" else "içeren"
        facts = {
            **base_facts,
            "match_type": query.mode,
            "query": query.value,
            "matched_count": len(matched),
            "matched_names": matched,
        }
        lines = [f'`{query.value}` {relation} namespace sayısı: **{len(matched)}**']
        if query.list_names:
            lines.extend(["", "Namespace'ler:", *[f"- `{name}`" for name in matched]])
        return AgentResult(
            "\n".join(lines), [{"name": "k8s_list", "status": "success"}],
            [_envelope(cluster_id, result, facts)], 0,
        )

    found = names_by_folded.get(query.value.casefold())
    if found:
        name, item = found
        phase = _namespace_phase(item)
        facts = {**base_facts, "exists": True, "name": name, "phase": phase}
        phase_line = f"\n- Phase: **{phase}**" if phase else ""
        answer = f"`{name}` namespace mevcut.{phase_line}"
    else:
        suggestions = namespace_suggestions(query.value, names, complete=True)
        facts = {
            **base_facts, "exists": False, "name": query.value,
            "suggestions": suggestions,
        }
        answer = f"`{query.value}` namespace bulunamadı."
        if suggestions:
            answer += "\n\nŞunu mu demek istediniz?\n" + "\n".join(
                f"- `{candidate}`" for candidate in suggestions
            )
    return AgentResult(
        answer, [{"name": "k8s_list", "status": "success"}],
        [_envelope(cluster_id, result, facts)], 0,
    )
