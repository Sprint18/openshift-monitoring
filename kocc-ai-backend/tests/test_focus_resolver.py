from __future__ import annotations

import pytest

from app.focus_resolver import resolve_semantic_focus
from app.llm_client import LLMUnavailable
from tests.test_agent import FakeLLM


def resolve(response: str | Exception) -> tuple[str | None, FakeLLM]:
    llm = FakeLLM([
        response if isinstance(response, Exception) else {
            "content": response, "tool_calls": None,
        }
    ])
    result = resolve_semantic_focus(
        llm,
        question="bunlar içinde en kritik olan hangisi?",
        answer="Yorum: openshift-storage seçildi; lab-sdlc de incelendi.",
        cluster_id="kkbtest",
        cluster_name="KKB TEST",
    )
    return result, llm


def test_focus_resolver_returns_one_valid_namespace_without_tools() -> None:
    namespace, llm = resolve('{"namespace":"openshift-storage"}')
    assert namespace == "openshift-storage"
    assert llm.calls[0]["tools"] is None
    assert len(llm.calls[0]["messages"]) == 2
    assert "openshift-storage seçildi" in llm.calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("content", [
    '{"namespace":null}',
    "not-json",
    '{"namespace":"KK B TEST"}',
    '{"namespace":"kkbtest"}',
    '{"namespace":"' + "a" * 64 + '"}',
    '{"namespace":["lab-sdlc","dynatrace"]}',
    '{"namespace":{"value":"lab-sdlc"}}',
    '{"namespace":"lab-sdlc","other":true}',
])
def test_focus_resolver_fails_closed_for_invalid_output(content: str) -> None:
    namespace, _llm = resolve(content)
    assert namespace is None


@pytest.mark.parametrize("failure", [
    LLMUnavailable("timeout"),
    LLMUnavailable("unavailable"),
])
def test_focus_resolver_unavailable_returns_no_focus(failure: Exception) -> None:
    namespace, _llm = resolve(failure)
    assert namespace is None


def test_focus_resolver_inputs_are_bounded() -> None:
    llm = FakeLLM([{"content": '{"namespace":null}', "tool_calls": None}])
    resolve_semantic_focus(
        llm,
        question="q" * 5000,
        answer="a" * 10000,
        cluster_id="kkbtest",
        cluster_name="KKB TEST",
    )
    user_payload = llm.calls[0]["messages"][1]["content"]
    assert len(user_payload) < 5300
