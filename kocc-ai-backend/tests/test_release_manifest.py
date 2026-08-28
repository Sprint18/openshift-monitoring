from pathlib import Path


def test_test_manifest_uses_validated_llm_timeout() -> None:
    manifest = (
        Path(__file__).parents[1]
        / "openshift"
        / "kocc-ai-backend-test.yaml"
    ).read_text()
    assert 'AI_LLM_TIMEOUT_SECONDS: "45"' in manifest
