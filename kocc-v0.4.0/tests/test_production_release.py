from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROD_MANIFEST = PROJECT_ROOT / "openshift" / "kocc-prod.yaml"
PROD_NAMESPACE = "ocp-monitoring-portal-prod"


def production_resources() -> dict[str, dict]:
    return {
        item["kind"]: item
        for item in yaml.safe_load_all(PROD_MANIFEST.read_text())
    }


def test_production_namespace_service_account_and_replica_sizing() -> None:
    resources = production_resources()
    for kind in ("ServiceAccount", "ImageStream", "BuildConfig", "Deployment", "Service", "Route"):
        assert resources[kind]["metadata"]["namespace"] == PROD_NAMESPACE
    deployment = resources["Deployment"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "kocc"
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }


def test_production_secret_mount_and_dynatrace_defense_in_depth() -> None:
    pod_template = production_resources()["Deployment"]["spec"]["template"]
    container = pod_template["spec"]["containers"][0]
    assert container["volumeMounts"] == [{
        "name": "remote-clusters", "mountPath": "/etc/portal/clusters",
        "readOnly": True,
    }]
    assert pod_template["spec"]["volumes"] == [{
        "name": "remote-clusters",
        "secret": {"secretName": "kocc-remote-clusters", "defaultMode": 0o440},
    }]
    assert pod_template["metadata"]["annotations"] == {
        "dynatrace.com/inject": "false",
        "oneagent.dynatrace.com/inject": "false",
        "metadata-enrichment.dynatrace.com/inject": "false",
    }


def test_production_rbac_is_read_only_and_includes_egressip() -> None:
    role = production_resources()["ClusterRole"]
    forbidden = {"create", "update", "patch", "delete"}
    assert role["metadata"]["name"] == "kocc-prod-readonly"
    assert all(forbidden.isdisjoint(rule["verbs"]) for rule in role["rules"])
    assert any(
        rule["apiGroups"] == ["k8s.ovn.org"]
        and rule["resources"] == ["egressips"]
        and rule["verbs"] == ["get", "list"]
        for rule in role["rules"]
    )


def test_production_runtime_has_no_test_cluster_references() -> None:
    runtime_text = "\n".join(
        path.read_text()
        for path in sorted((PROJECT_ROOT / "app").rglob("*"))
        if path.is_file() and path.suffix in {".py", ".html", ".js"}
    ) + PROD_MANIFEST.read_text()
    for forbidden in (
        "kkbtest", "rmtest", "kkbocptest", "rmocptest",
        "ocp-monitoring-portal-test", "rmtest.kubeconfig",
    ):
        assert forbidden not in runtime_text.lower()


def test_remote_cluster_rbac_standard_is_read_only() -> None:
    manifest = PROJECT_ROOT / "openshift" / "kocc-remote-readonly.yaml"
    resources = list(yaml.safe_load_all(manifest.read_text()))
    by_kind = {item["kind"]: item for item in resources}
    assert by_kind["Namespace"]["metadata"]["name"] == "prod-portal-integration"
    assert by_kind["ServiceAccount"]["metadata"] == {
        "name": "portal-monitor", "namespace": "prod-portal-integration",
    }
    role = by_kind["ClusterRole"]
    assert role["metadata"]["name"] == "portal-monitor-readonly"
    forbidden = {"create", "update", "patch", "delete"}
    assert all(forbidden.isdisjoint(rule["verbs"]) for rule in role["rules"])
