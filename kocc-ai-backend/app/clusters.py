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


ALL_CLUSTER_PHRASES = (
    "tüm clusterlara", "tüm clusterları", "bütün clusterlara",
    "bütün clusterları", "hepsinin", "all clusters", "check all clusters",
)
TURKISH_SUFFIXES = (
    "de", "da", "te", "ta", "e", "a", "in", "ın", "un", "ün",
    "nin", "nın", "nun", "nün",
)


def explicit_cluster_scope(
    message: str, registry: dict[str, Cluster]
) -> ClusterScope | None:
    normalized = " ".join(message.casefold().replace("’", "'").split())
    if any(phrase in normalized for phrase in ALL_CLUSTER_PHRASES):
        return ClusterScope(
            "all", tuple(item.id for item in registry.values() if item.enabled)
        )
    matches: list[str] = []
    suffix = "|".join(TURKISH_SUFFIXES)
    for cluster in registry.values():
        if not cluster.enabled:
            continue
        for alias in cluster.aliases:
            pattern = rf"(?<!\w){re.escape(alias)}(?:'?(?:{suffix}))?(?!\w)"
            if re.search(pattern, normalized):
                matches.append(cluster.id)
                break
    unique = tuple(cluster_id for cluster_id in registry if cluster_id in matches)
    if not unique:
        return None
    return ClusterScope("single" if len(unique) == 1 else "multiple", unique)


class UnknownClusterError(ValueError):
    pass


def selected_cluster(registry: dict[str, Cluster], cluster_id: str) -> Cluster:
    cluster = registry.get(cluster_id)
    if cluster is None or not cluster.enabled:
        raise UnknownClusterError("Unknown or disabled cluster")
    return cluster
