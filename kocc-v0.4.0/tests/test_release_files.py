from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def manifest_resources() -> dict[str, dict]:
    manifest = PROJECT_ROOT / "openshift" / "kocc-v0.4.0.yaml"
    resources = yaml.safe_load_all(manifest.read_text())
    return {resource["kind"]: resource for resource in resources}


def test_deployment_preserves_oneagent_and_readable_secret_volume() -> None:
    deployment = manifest_resources()["Deployment"]
    pod_template = deployment["spec"]["template"]
    secret = pod_template["spec"]["volumes"][0]["secret"]

    assert pod_template["metadata"]["annotations"] == {
        "oneagent.dynatrace.com/inject": "true"
    }
    assert secret["defaultMode"] == 0o440


def test_base_image_is_pinned_by_digest() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    first_line = dockerfile.splitlines()[0]
    assert first_line.startswith("FROM registry.access.redhat.com/ubi9/python-312:9.6@sha256:")
