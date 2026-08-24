from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kubernetes import client, config


REMOTE_CLUSTER_DIR = Path("/etc/portal/clusters")


@dataclass(frozen=True)
class ClusterDefinition:
    key: str
    name: str
    connection_type: str
    kubeconfig_context: str | None = None

    @property
    def kubeconfig_path(self) -> Path | None:
        if self.connection_type != "kubeconfig":
            return None
        return REMOTE_CLUSTER_DIR / f"{self.key}.kubeconfig"


CLUSTERS: dict[str, ClusterDefinition] = {
    "ocptrdprod1": ClusterDefinition(
        key="ocptrdprod1",
        name="OCPTRDPROD1",
        connection_type="incluster",
    ),
    "ocptrdprod2": ClusterDefinition(
        key="ocptrdprod2",
        name="OCPTRDPROD2",
        connection_type="kubeconfig",
    ),
    "ocptrddr1": ClusterDefinition(
        key="ocptrddr1",
        name="OCPTRDDR1",
        connection_type="kubeconfig",
    ),
}

DEFAULT_CLUSTER = "ocptrdprod1"


class ClusterNotFoundError(ValueError):
    pass


class ClusterConfigurationError(RuntimeError):
    pass


def get_cluster_definitions() -> dict[str, ClusterDefinition]:
    return CLUSTERS.copy()


def get_cluster_definition(cluster_key: str) -> ClusterDefinition:
    try:
        return CLUSTERS[cluster_key]
    except KeyError as exc:
        supported = ", ".join(sorted(CLUSTERS))
        raise ClusterNotFoundError(
            f"Desteklenmeyen cluster: {cluster_key}. Desteklenen cluster'lar: {supported}"
        ) from exc


def new_cluster_client(cluster_key: str) -> client.ApiClient:
    definition = get_cluster_definition(cluster_key)

    if definition.connection_type == "incluster":
        try:
            config.load_incluster_config()
            return client.ApiClient()
        except Exception as exc:
            raise ClusterConfigurationError(
                f"{definition.name} için in-cluster configuration yüklenemedi."
            ) from exc

    if definition.connection_type == "kubeconfig":
        if not definition.kubeconfig_path:
            raise ClusterConfigurationError(
                f"{definition.name} için kubeconfig yolu tanımlı değil."
            )

        kubeconfig_path = definition.kubeconfig_path
        if not kubeconfig_path.is_file():
            raise ClusterConfigurationError(
                f"{definition.name} kubeconfig dosyası bulunamadı: {kubeconfig_path}"
            )

        try:
            return config.new_client_from_config(
                config_file=str(kubeconfig_path),
                context=definition.kubeconfig_context,
            )
        except Exception as exc:
            raise ClusterConfigurationError(
                f"{definition.name} kubeconfig yüklenemedi."
            ) from exc

    raise ClusterConfigurationError(
        f"{definition.name} için bilinmeyen bağlantı tipi: "
        f"{definition.connection_type}"
    )
