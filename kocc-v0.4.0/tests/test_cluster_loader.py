from unittest.mock import Mock, patch

import pytest

from app.cluster_loader import (
    DEFAULT_CLUSTER,
    REMOTE_CLUSTER_DIR,
    ClusterConfigurationError,
    ClusterNotFoundError,
    get_cluster_definition,
    get_cluster_definitions,
    new_cluster_client,
)


def test_production_cluster_registry_and_generic_paths() -> None:
    definitions = get_cluster_definitions()
    assert DEFAULT_CLUSTER == "ocptrdprod1"
    assert list(definitions) == ["ocptrdprod1", "ocptrdprod2", "ocptrddr1"]
    assert definitions["ocptrdprod1"].connection_type == "incluster"
    for key in ("ocptrdprod2", "ocptrddr1"):
        assert definitions[key].connection_type == "kubeconfig"
        assert definitions[key].kubeconfig_path == (
            REMOTE_CLUSTER_DIR / f"{key}.kubeconfig"
        )


def test_unknown_cluster_is_rejected() -> None:
    with pytest.raises(ClusterNotFoundError):
        get_cluster_definition("unknown")


@patch("app.cluster_loader.client.ApiClient")
@patch("app.cluster_loader.config.load_incluster_config")
def test_local_cluster_uses_incluster_config(
    load_incluster_config: Mock,
    api_client: Mock,
) -> None:
    result = new_cluster_client("ocptrdprod1")

    load_incluster_config.assert_called_once_with()
    api_client.assert_called_once_with()
    assert result is api_client.return_value


@patch("app.cluster_loader.Path.is_file", return_value=True)
@patch("app.cluster_loader.config.new_client_from_config")
def test_remote_cluster_uses_only_kubeconfig_client(
    new_client_from_config: Mock,
    _is_file: Mock,
) -> None:
    result = new_cluster_client("ocptrdprod2")

    new_client_from_config.assert_called_once_with(
        config_file="/etc/portal/clusters/ocptrdprod2.kubeconfig",
        context=None,
    )
    assert result is new_client_from_config.return_value


@patch("app.cluster_loader.Path.is_file", return_value=True)
@patch("app.cluster_loader.config.new_client_from_config")
def test_disaster_recovery_cluster_uses_generic_kubeconfig_path(
    new_client_from_config: Mock,
    _is_file: Mock,
) -> None:
    new_cluster_client("ocptrddr1")
    new_client_from_config.assert_called_once_with(
        config_file="/etc/portal/clusters/ocptrddr1.kubeconfig",
        context=None,
    )


@patch("app.cluster_loader.Path.is_file", return_value=False)
def test_missing_remote_kubeconfig_is_reported(_is_file: Mock) -> None:
    with pytest.raises(ClusterConfigurationError):
        new_cluster_client("ocptrdprod2")
