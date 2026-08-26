import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET_NAMESPACE = "ocp-monitoring-portal-test"
IMAGE_PULLSPEC = (
    "image-registry.openshift-image-registry.svc:5000/"
    "ocp-monitoring-portal-test/kocc:0.4.0"
)


def manifest_resources() -> dict[str, dict]:
    manifest = PROJECT_ROOT / "openshift" / "kocc-v0.4.0.yaml"
    resources = yaml.safe_load_all(manifest.read_text())
    return {resource["kind"]: resource for resource in resources}


def test_namespaced_resources_target_the_deployment_project() -> None:
    resources = manifest_resources()

    for kind in (
        "ServiceAccount",
        "ImageStream",
        "BuildConfig",
        "Deployment",
        "Service",
        "Route",
    ):
        assert resources[kind]["metadata"]["namespace"] == TARGET_NAMESPACE

    binding = resources["ClusterRoleBinding"]
    assert binding["subjects"][0]["namespace"] == TARGET_NAMESPACE


def test_cluster_operator_rbac_is_read_only_and_minimal() -> None:
    role = manifest_resources()["ClusterRole"]
    operator_rules = [
        rule
        for rule in role["rules"]
        if "clusteroperators" in rule.get("resources", [])
    ]
    assert operator_rules == [
        {
            "apiGroups": ["config.openshift.io"],
            "resources": ["clusteroperators"],
            "verbs": ["get", "list"],
        }
    ]


def test_diagnostics_rbac_is_strictly_read_only() -> None:
    role = manifest_resources()["ClusterRole"]
    rules = role["rules"]
    pod_log_rule = next(
        rule for rule in rules if "pods/log" in rule.get("resources", [])
    )
    event_rule = next(
        rule for rule in rules if "events" in rule.get("resources", [])
    )

    assert pod_log_rule["verbs"] == ["get"]
    assert event_rule["verbs"] == ["get", "list"]
    forbidden = {"create", "delete", "patch", "update"}
    assert all(
        forbidden.isdisjoint(rule.get("verbs", [])) for rule in rules
    )

def test_build_and_deployment_use_the_same_image_stream_tag() -> None:
    resources = manifest_resources()
    build_output = resources["BuildConfig"]["spec"]["output"]["to"]
    deployment = resources["Deployment"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    trigger = json.loads(
        deployment["metadata"]["annotations"]["image.openshift.io/triggers"]
    )[0]

    assert build_output == {
        "kind": "ImageStreamTag",
        "name": "kocc:0.4.0",
        "namespace": TARGET_NAMESPACE,
    }
    assert container["image"] == IMAGE_PULLSPEC
    assert container["imagePullPolicy"] == "Always"
    assert trigger["from"] == build_output
    assert trigger["fieldPath"] == (
        'spec.template.spec.containers[?(@.name=="kocc")].image'
    )
    assert trigger["paused"] is False


def test_deployment_excludes_dynatrace_and_preserves_secret_volume() -> None:
    deployment = manifest_resources()["Deployment"]
    pod_template = deployment["spec"]["template"]
    secret = pod_template["spec"]["volumes"][0]["secret"]
    container = pod_template["spec"]["containers"][0]

    assert pod_template["metadata"]["annotations"] == {
        "dynatrace.com/inject": "false",
    }
    assert all(
        env.get("name") != "LD_PRELOAD"
        for env in container.get("env", [])
    )
    assert secret["secretName"] == "kocc-remote-clusters"
    assert secret["items"] == [
        {"key": "rmtest.kubeconfig", "path": "rmtest.kubeconfig"}
    ]
    assert secret["defaultMode"] == 0o440


def test_test_deployment_mounts_existing_sqlite_pvc_with_fs_group() -> None:
    deployment = manifest_resources()["Deployment"]
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert pod_spec["securityContext"] == {"fsGroup": 1002940000}
    assert {"name": "kocc-data", "mountPath": "/data"} in container["volumeMounts"]
    assert {
        "name": "kocc-data",
        "persistentVolumeClaim": {"claimName": "kocc-data"},
    } in pod_spec["volumes"]


def test_secret_template_targets_the_deployment_project() -> None:
    secret_template = PROJECT_ROOT / "openshift" / "rmtest-secret-template.yaml"
    secret = yaml.safe_load(secret_template.read_text())

    assert secret["metadata"]["namespace"] == TARGET_NAMESPACE


def test_base_image_is_pinned_by_digest() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    first_line = dockerfile.splitlines()[0]
    assert first_line.startswith("FROM registry.access.redhat.com/ubi9/python-312:9.6@sha256:")
