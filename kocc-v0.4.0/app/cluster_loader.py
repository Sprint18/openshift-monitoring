from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kubernetes import client, config


@dataclass(frozen=True)
class ClusterDefinition:
    key: str
    name: str
    connection_type: str
    kubeconfig_path: str | None = None
    kubeconfig_context: str | None = None


CLUSTERS: dict[str, ClusterDefinition] = {
    "kkbtest": ClusterDefinition(
        key="kkbtest",
        name="KKBTEST",
        connection_type="incluster",
    ),
    "rmtest": ClusterDefinition(
        key="rmtest",
        name="RMTEST",
        connection_type="kubeconfig",
        kubeconfig_path="/etc/portal/clusters/rmtest.kubeconfig",
    ),
}

DEFAULT_CLUSTER = "kkbtest"


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

        kubeconfig_path = Path(definition.kubeconfig_path)
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
