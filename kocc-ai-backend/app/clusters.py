from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    enabled: bool
    mcp_url: str
    aliases: tuple[str, ...]

    def public_dict(self) -> dict[str, str | bool]:
        return {"id": self.id, "name": self.name, "enabled": self.enabled}


def cluster_registry(settings: Settings) -> dict[str, Cluster]:
    return {
        "kkbtest": Cluster(
            id="kkbtest", name="KKB TEST", enabled=True,
            mcp_url=settings.mcp_kkbtest_url,
            aliases=("kkbtest", "kkb test"),
        ),
        "rmtest": Cluster(
            id="rmtest", name="RMTEST", enabled=True,
            mcp_url=settings.mcp_rmtest_url,
            aliases=("rmtest", "rm test", "rmocptest1"),
        ),
    }


@dataclass(frozen=True)
class ClusterScope:
    kind: str
    cluster_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedClusterRequest:
    scope: ClusterScope
    operational_message: str


ALL_CLUSTER_PHRASES = (
    "tüm clusterlara", "tüm clusterları", "bütün clusterlara",
    "bütün clusterları", "hepsinin", "all clusters", "check all clusters",
)
TURKISH_SUFFIXES = (
    "teki", "taki", "deki", "daki", "de", "da", "te", "ta", "e", "a",
    "in", "ın", "un", "ün", "nin", "nın", "nun", "nün",
)
CLUSTER_WORD_SUFFIXES = (
    "ındaki", "indeki", "undaki", "ündeki", "ında", "inde", "unda", "ünde",
    "ına", "ine", "una", "üne", "ın", "in", "un", "ün", "da", "de",
    "ta", "te",
)


def _clean_operational_message(message: str, spans: list[tuple[int, int]]) -> str:
    chars = list(message)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return " ".join("".join(chars).split()).strip(" ,;:-")


def resolve_cluster_request(
    message: str, registry: dict[str, Cluster]
) -> ResolvedClusterRequest | None:
    search_text = message.replace("’", "'")
    for phrase in ALL_CLUSTER_PHRASES:
        match = re.search(re.escape(phrase), search_text, flags=re.IGNORECASE)
        if match:
            return ResolvedClusterRequest(
                ClusterScope(
                    "all", tuple(item.id for item in registry.values() if item.enabled)
                ),
                _clean_operational_message(message, [match.span()]),
            )

    matches: list[tuple[str, tuple[int, int]]] = []
    suffix = "|".join(TURKISH_SUFFIXES)
    cluster_suffix = "|".join(CLUSTER_WORD_SUFFIXES)
    for cluster in registry.values():
        if not cluster.enabled:
            continue
        for alias in cluster.aliases:
            pattern = (
                rf"(?<!\w){re.escape(alias)}(?:'?(?:{suffix}))?"
                rf"(?:\s+cluster(?:{cluster_suffix})?)?(?!\w)"
            )
            match = re.search(pattern, search_text, flags=re.IGNORECASE)
            if match:
                matches.append((cluster.id, match.span()))
                break
    cluster_ids = tuple(
        cluster_id for cluster_id in registry
        if any(match_id == cluster_id for match_id, _span in matches)
    )
    if not cluster_ids:
        return None
    return ResolvedClusterRequest(
        ClusterScope("single" if len(cluster_ids) == 1 else "multiple", cluster_ids),
        _clean_operational_message(message, [span for _cluster_id, span in matches]),
    )


def explicit_cluster_scope(
    message: str, registry: dict[str, Cluster]
) -> ClusterScope | None:
    resolved = resolve_cluster_request(message, registry)
    return resolved.scope if resolved else None


class UnknownClusterError(ValueError):
    pass


def selected_cluster(registry: dict[str, Cluster], cluster_id: str) -> Cluster:
    cluster = registry.get(cluster_id)
    if cluster is None or not cluster.enabled:
        raise UnknownClusterError("Unknown or disabled cluster")
    return cluster
