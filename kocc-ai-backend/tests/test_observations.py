from __future__ import annotations

import json

from app.agent import AgentLoop, SYSTEM_PROMPT, TRUNCATION_MARKER
from app.observations import cluster_operator_facts, deterministic_observation
from tests.test_agent import FakeLLM, FakeMCP, configured, tool_call


def operator(name: str, degraded: bool = False) -> dict:
    return {
        "apiVersion": "config.openshift.io/v1",
        "kind": "ClusterOperator",
        "metadata": {"name": name},
        "status": {"conditions": [
            {"type": "Available", "status": "True"},
            {"type": "Progressing", "status": "False"},
            {"type": "Degraded", "status": "True" if degraded else "False"},
        ]},
    }


def test_generic_kubernetes_list_count_is_deterministic() -> None:
    result = {"kind": "PodList", "items": [{"kind": "Pod"} for _ in range(34)]}
    assert deterministic_observation("resources_list", {"kind": "Pod"}, result) == {
        "resource_count": 34,
        "kind": "Pod",
    }


def test_cluster_operator_summary_counts_conditions() -> None:
    items = [operator(f"operator-{index}") for index in range(34)]
    facts = deterministic_observation(
        "resources_list", {"kind": "ClusterOperator"}, {"items": items}
    )
    assert facts == {
        "resource_count": 34,
        "kind": "ClusterOperator",
        "degraded_true_count": 0,
        "available_false_count": 0,
        "progressing_true_count": 0,
    }

    items[-1] = operator("degraded", degraded=True)
    facts = deterministic_observation(
        "resources_list", {"kind": "ClusterOperator"}, {"items": items}
    )
    assert facts["resource_count"] == 34
    assert facts["degraded_true_count"] == 1


def operator_table(count: int = 34) -> str:
    header = (
        "APIVERSION KIND NAME VERSION AVAILABLE PROGRESSING DEGRADED "
        "SINCE MESSAGE LABELS"
    )
    rows = [
        f"config.openshift.io/v1 ClusterOperator co-{index} 4.18.40 "
        "True False False 1d ok -"
        for index in range(count)
    ]
    return "\n".join([header, "", *rows])


def test_cluster_operator_table_parser_excludes_header_blank_and_invalid_rows() -> None:
    table = operator_table() + "\ninvalid row\nconfig.openshift.io/v1 ClusterOperator bad 4.18 Maybe False False"
    facts = cluster_operator_facts({"content": [{"type": "text", "text": table}]}, "rmtest")
    assert facts is not None
    assert facts.cluster_id == "rmtest"
    assert facts.total == 34
    assert facts.available_false_count == 0
    assert facts.progressing_true_count == 0
    assert facts.degraded_true_count == 0


def test_cluster_operator_table_parser_tolerates_sse_and_jsonrpc_wrappers() -> None:
    envelope = json.dumps({
        "jsonrpc": "2.0", "id": 7,
        "result": {"content": [{"type": "text", "text": operator_table()}]},
    })
    wrapped = f"event: message\ndata: {envelope}\n\n"
    facts = cluster_operator_facts({
        "content": [{"type": "text", "text": wrapped}]
    }, "rmtest")
    assert facts is not None
    assert facts.total == 34


def test_cluster_operator_table_conditions_use_only_valid_records() -> None:
    table = "\n".join([
        operator_table(31),
        "config.openshift.io/v1 ClusterOperator unavailable 4.18.40 False False False 1d msg -",
        "config.openshift.io/v1 ClusterOperator progressing 4.18.40 True True False 1d msg -",
        "config.openshift.io/v1 ClusterOperator degraded 4.18.40 True False True 1d msg -",
        "config.openshift.io/v1 ClusterOperator invalid 4.18.40 Unknown True True 1d msg -",
    ])
    facts = cluster_operator_facts({"content": [{"text": table}]})
    assert facts is not None
    assert facts.total == 34
    assert facts.available_false_count == 1
    assert facts.progressing_true_count == 1
    assert facts.degraded_true_count == 1
    assert facts.unavailable_names == ("unavailable",)
    assert facts.progressing_names == ("progressing",)
    assert facts.degraded_names == ("degraded",)


def test_cluster_operator_structured_parser_counts_only_valid_records() -> None:
    items = [operator(f"valid-{index}") for index in range(34)]
    items.extend([
        {"kind": "ClusterOperator", "metadata": {"name": "header"}},
        {"apiVersion": "config.openshift.io/v1", "kind": "Pod",
         "metadata": {"name": "wrong-kind"}},
    ])
    facts = cluster_operator_facts({"items": items}, "kkbtest")
    assert facts is not None
    assert facts.total == 34


def test_observation_reads_streamable_mcp_text_and_structured_content() -> None:
    payload = {"kind": "NodeList", "items": [{"kind": "Node"}] * 3}
    text_result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    structured_result = {"structuredContent": payload}
    assert deterministic_observation("resources_list", {}, text_result)["resource_count"] == 3
    assert deterministic_observation("resources_list", {}, structured_result)["resource_count"] == 3


def test_authoritative_facts_are_computed_before_raw_result_truncation() -> None:
    items = [operator(f"operator-{index}") for index in range(34)]
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call(
            "resources_list", '{"kind":"ClusterOperator"}'
        )]},
        {"content": "34 operators, zero degraded", "tool_calls": None},
    ])
    result = AgentLoop(
        configured(agent_max_tool_result_chars=400),
        llm,
        FakeMCP([{"items": items, "padding": "x" * 50000}]),
    ).run("Kubernetes kaynak listesini özetle")
    tool_context = llm.calls[1]["messages"][-1]["content"]
    assert '"resource_count":34' in tool_context
    assert '"degraded_true_count":0' in tool_context
    assert TRUNCATION_MARKER in tool_context
    assert len(tool_context) == 400
    assert result.evidence == [{
        "tool": "resources_list",
        "status": "success",
        "facts": {
            "resource_count": 34,
            "degraded_true_count": 0,
            "available_false_count": 0,
            "progressing_true_count": 0,
        },
    }]


def test_prompt_makes_observations_authoritative_and_limits_tool_scope() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "deterministic observations" in prompt
    assert "authoritative" in prompt
    assert "never recount, alter, or contradict" in prompt
    assert "resource_count is 34" in prompt
    assert "minimum tools necessary" in prompt
    assert "without broadening scope" in prompt


def test_cluster_operator_question_stops_after_sufficient_resource_evidence() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call(
            "resources_list", '{"kind":"ClusterOperator"}'
        )]},
        {"content": "Degraded operator yok.", "tool_calls": None},
    ])
    mcp = FakeMCP([{"items": [operator("one")]}])
    result = AgentLoop(configured(), llm, mcp).run(
        "Degraded ClusterOperator var mı?"
    )
    assert "Toplam ClusterOperator: **1**" in result.answer
    assert "Degraded=True: **0**" in result.answer
    assert mcp.calls == [("resources_list", {
        "apiVersion": "config.openshift.io/v1", "kind": "ClusterOperator"
    })]
    assert llm.calls == []


def test_exact_failed_tool_call_is_not_executed_twice() -> None:
    from app.mcp_client import MCPUnavailable

    repeated = tool_call(
        "resources_list",
        '{"apiVersion":"config.openshift.io/v1","kind":"ClusterOperator"}',
    )
    llm = FakeLLM([
        {"content": None, "tool_calls": [repeated]},
        {"content": None, "tool_calls": [{**repeated, "id": "call-2"}]},
        {"content": "Bu bilgi mevcut araçlarla doğrulanamadı.", "tool_calls": None},
    ])
    mcp = FakeMCP([MCPUnavailable("timeout")])
    result = AgentLoop(configured(), llm, mcp).run("kaynakları incele")
    assert len(mcp.calls) == 1
    assert result.evidence == []
    assert result.tool_calls == [
        {"name": "resources_list", "status": "error"},
        {"name": "resources_list", "status": "error"},
    ]
