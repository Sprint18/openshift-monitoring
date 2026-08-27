from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    enabled: bool
    mcp_url: str

    def public_dict(self) -> dict[str, str | bool]:
        return {"id": self.id, "name": self.name, "enabled": self.enabled}


def cluster_registry(settings: Settings) -> dict[str, Cluster]:
    return {
        "kkbtest": Cluster(
            id="kkbtest", name="KKB TEST", enabled=True,
            mcp_url=settings.mcp_kkbtest_url,
        )
    }


class UnknownClusterError(ValueError):
    pass


def selected_cluster(registry: dict[str, Cluster], cluster_id: str) -> Cluster:
    cluster = registry.get(cluster_id)
    if cluster is None or not cluster.enabled:
        raise UnknownClusterError("Unknown or disabled cluster")
    return cluster
