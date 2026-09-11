from __future__ import annotations

import json

import pytest

from app.conversation import SafeTurn, TriageCandidate, TriageResource
from app.llm_client import LLMUnavailable
from app.triage import (
    TRIAGE_SYSTEM_PROMPT, TriageInvalid, decide_triage,
)
from tests.test_agent import FakeLLM


def candidates() -> tuple[TriageCandidate, ...]:
    return (
        TriageCandidate("dynatrace", (
            TriageResource(
                "Pod", "oneagent-a", "Pending", False, 0,
                ("FailedMount",),
            ),
            TriageResource(
                "Pod", "csi-driver-b", "Pending", False, 0,
                ("FailedScheduling",),
            ),
        )),
        TriageCandidate("lab-sdlc", (
            TriageResource(
                "Pod", "sonarqube-a", "Running", False, 12,
                ("CrashLoopBackOff",),
            ),
        )),
        TriageCandidate("uat-greendeks", (
            TriageResource(
                "Pod", "python-app-a", "Running", False, 5,
                ("CrashLoopBackOff",),
            ),
        )),
    )


def decision(**changes: object) -> str:
    payload: dict[str, object] = {
        "assessment": "highest",
        "selected_namespace": "dynatrace",
        "tied_namespaces": [],
        "selected_resources": ["oneagent-a", "csi-driver-b"],
        "confidence": "medium",
        "reasons": [
            "Birden fazla ilişkili kaynak etkileniyor.",
            "FailedMount ile CSI durumu olası bir teknik zincir oluşturuyor.",
        ],
    }
    payload.update(changes)
    return json.dumps(payload)


def test_structured_triage_uses_fixed_rubric_and_normalized_schema() -> None:
    llm = FakeLLM([{"content": decision(), "tool_calls": None}])
    result = decide_triage(
        llm, question="en kritik hangisi?", cluster_id="kkbtest",
        candidates=candidates(), semantic_history=[
            SafeTurn("assistant", "Önceki grounded özet"),
        ],
    )
    assert result.selected_namespace == "dynatrace"
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["messages"][0]["content"] == TRIAGE_SYSTEM_PROMPT
    prompt = json.loads(llm.calls[0]["messages"][1]["content"])
    assert set(prompt) == {
        "active_cluster_id", "question", "recent_semantic_context",
        "comparison_candidates",
    }
    assert prompt["comparison_candidates"][0] == {
        "namespace": "dynatrace",
        "resources": [{
            "kind": "Pod", "name": "oneagent-a", "state": "Pending",
            "ready": False, "restart_count": 0, "reasons": ["FailedMount"],
        }, {
            "kind": "Pod", "name": "csi-driver-b", "state": "Pending",
            "ready": False, "restart_count": 0,
            "reasons": ["FailedScheduling"],
        }],
    }
    assert "namespace naming" in TRIAGE_SYSTEM_PROMPT
    assert "business impact" in TRIAGE_SYSTEM_PROMPT
    assert "raw\ncumulative restart count" in TRIAGE_SYSTEM_PROMPT


def test_answer_is_rendered_from_same_selected_focus() -> None:
    result = decide_triage(
        FakeLLM([{"content": decision(), "tool_calls": None}]),
        question="hangisi?", cluster_id="kkbtest", candidates=candidates(),
    )
    assert "`dynatrace` seçildi" in result.render_answer()
    assert "lab-sdlc" not in result.render_answer()


@pytest.mark.parametrize("content", [
    "not-json",
    decision(selected_namespace=None),
    decision(selected_namespace="a" * 64),
    decision(selected_namespace="invalid/name"),
    decision(selected_namespace="kkbtest"),
    decision(selected_namespace="not-observed"),
    json.dumps({"assessment": "highest"}),
    json.dumps({
        "assessment": "highest", "selected_namespace": "dynatrace",
        "tied_namespaces": [], "selected_resources": [],
        "confidence": "medium", "reasons": ["reason"], "extra": True,
    }),
])
def test_malformed_triage_is_rejected(content: str) -> None:
    with pytest.raises(TriageInvalid):
        decide_triage(
            FakeLLM([{"content": content, "tool_calls": None}]),
            question="hangisi?", cluster_id="kkbtest", candidates=candidates(),
        )


def test_unavailable_triage_does_not_invent_winner() -> None:
    with pytest.raises(LLMUnavailable):
        decide_triage(
            FakeLLM([LLMUnavailable("timeout")]), question="hangisi?",
            cluster_id="kkbtest", candidates=candidates(),
        )


def test_valid_tie_has_no_selected_focus() -> None:
    content = decision(
        assessment="tie", selected_namespace=None,
        tied_namespaces=["dynatrace", "lab-sdlc"], selected_resources=[],
        confidence="low", reasons=["Doğrudan kanıtlar ayrım için yetersiz."],
    )
    result = decide_triage(
        FakeLLM([{"content": content, "tool_calls": None}]),
        question="hangisi?", cluster_id="kkbtest", candidates=candidates(),
    )
    assert result.selected_namespace is None
    assert result.tied_namespaces == ("dynatrace", "lab-sdlc")
    assert "birbirine yakın" in result.render_answer()
