from __future__ import annotations

import pytest

from app.agent import AgentLoop, SYSTEM_PROMPT
from tests.test_agent import FakeLLM, FakeMCP, configured


def guarded(answer: str) -> str:
    return AgentLoop(
        configured(), FakeLLM([{"content": answer, "tool_calls": None}]),
        FakeMCP(), "kkbtest", "KKB TEST",
    ).run("operasyonel bulguyu değerlendir").answer


@pytest.mark.parametrize(("claim", "forbidden", "expected"), [
    (
        "Pod 83 gündür down.", "83 gündür down",
        "Pod 83 günlük ve mevcut incelemede problemli durumda",
    ),
    (
        "Container 237 kez restart etmiş; tüm servis down.",
        "tüm servis down", "servis etkisi mevcut araçlarla doğrulanamadı",
    ),
    (
        "Bu pod hazır değil ve uygulama tamamen kullanılamaz.",
        "uygulama tamamen kullanılamaz",
        "servis etkisi mevcut araçlarla doğrulanamadı",
    ),
    (
        "prod-payments namespace adı business impact açısından kritiktir.",
        "business impact açısından kritiktir",
        "Namespace adı tek başına iş etkisini doğrulamaz",
    ),
    (
        "Dynatrace sorunu uygulama trafiğini etkilemiyor.",
        "uygulama trafiğini etkilemiyor",
        "Doğrudan uygulama trafik etkisi mevcut kanıtla doğrulanmadı",
    ),
    (
        "Tek düzeltme büyük ihtimalle ikisini birden toparlar.",
        "ikisini birden toparlar",
        "Aynı düzeltmenin iki sorunu da gidereceği mevcut kanıtla doğrulanmadı",
    ),
])
def test_unsupported_operational_claims_are_downgraded(
    claim: str, forbidden: str, expected: str,
) -> None:
    answer = guarded(claim)
    assert forbidden.casefold() not in answer.casefold()
    assert expected in answer


def test_unproven_failedmount_imagepull_relationship_is_labeled_analysis() -> None:
    answer = guarded(
        "SonarQube buna bağımlı olduğu için CrashLoopBackOff'ta; "
        "PostgreSQL ImagePullBackOff ve FailedMount durumunda."
    )
    assert "Yorum:" in answer
    assert "ImagePullBackOff" in answer
    assert "FailedMount" in answer


def test_manifest_unknown_supports_only_requested_manifest_not_found() -> None:
    claim = (
        "Registry `manifest unknown` bildirdi; istenen manifest/tag bulunamadı. "
        "Bu bulgu tek başına network veya authentication ihtimalini dışlamaz."
    )
    assert guarded(claim) == claim


def test_prompt_defines_observation_inference_and_impact_boundaries() -> None:
    assert "Pod age is creation age, never outage duration" in SYSTEM_PROMPT
    assert "Restart count indicates\ninstability" in SYSTEM_PROMPT
    assert "does not prove its\nwhole Service or application is down" in SYSTEM_PROMPT
    assert "do not prove business\ncriticality or user impact" in SYSTEM_PROMPT
    assert "manifest unknown" in SYSTEM_PROMPT
