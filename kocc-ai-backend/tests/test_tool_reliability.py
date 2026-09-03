from __future__ import annotations

import io
import logging

import pytest

from app.agent import AgentLoop, SYSTEM_PROMPT, openai_tools
from app.mcp_client import MCPUnavailable
from app.tool_contracts import validate_tool_arguments
from tests.test_agent import FakeLLM, FakeMCP, configured, tool_call
from tests.test_observations import operator, operator_table


RESOURCE_TOOLS = [{
    "name": "resources_list",
    "description": "List resources by apiVersion and kind",
    "inputSchema": {
        "type": "object",
        "properties": {
            "apiVersion": {"type": "string", "description": "API version"},
            "kind": {"type": "string", "description": "Resource kind"},
            "namespace": {"type": "string"},
            "labelSelector": {"type": "string"},
            "fieldSelector": {"type": "string"},
        },
        "required": ["apiVersion", "kind"],
    },
}, {
    "name": "events_list",
    "description": "List events",
    "inputSchema": {"type": "object", "properties": {}},
}]


def mcp_with(results):
    mcp = FakeMCP(results)
    mcp.list_tools = lambda: RESOURCE_TOOLS
    return mcp


def test_real_resources_list_schema_is_preserved_and_validates_required_fields() -> None:
    converted = openai_tools(RESOURCE_TOOLS)
    schema = converted[0]["function"]["parameters"]
    assert schema is RESOURCE_TOOLS[0]["inputSchema"]
    assert schema["required"] == ["apiVersion", "kind"]
    assert schema["properties"]["apiVersion"]["description"] == "API version"
    assert validate_tool_arguments({"kind": "Pod"}, schema) == (
        "Tool arguments are missing required fields."
    )
    assert validate_tool_arguments({
        "apiVersion": "v1", "kind": "Pod", "group": "invented"
    }, schema) == "Tool arguments contain unknown fields."


def test_cluster_operator_arguments_are_canonical_before_mcp_call() -> None:
    llm = FakeLLM([{"content": None, "tool_calls": [tool_call(
        "resources_list",
        '{"apiVersion":"operator.openshift.io/v1","kind":"ClusterOperator"}',
    )]}])
    mcp = mcp_with([{"content": [{"type": "text", "text": operator_table()}]}])
    result = AgentLoop(configured(), llm, mcp).run(
        "Degraded ClusterOperator var mı?"
    )
    assert mcp.calls == [("resources_list", {
        "apiVersion": "config.openshift.io/v1", "kind": "ClusterOperator"
    })]
    assert "Toplam ClusterOperator: **34**" in result.answer
    assert "operator.openshift.io" not in result.answer
    assert result.evidence[0]["facts"]["degraded_true_count"] == 0


def test_direct_cluster_operator_path_does_not_use_llm_prose() -> None:
    llm = FakeLLM([{"content": "Toplam 35 ClusterOperator", "tool_calls": None}])
    mcp = mcp_with([{"content": [{"type": "text", "text": operator_table()}]}])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("kocc_ai").addHandler(handler)
    try:
        result = AgentLoop(
            configured(), llm, mcp, "rmtest", "RMTEST"
        ).run("degraded CO var mı?")
    finally:
        logging.getLogger("kocc_ai").removeHandler(handler)
    logs = stream.getvalue()
    assert llm.calls == []
    assert result.answer.startswith("## RMTEST")
    assert "Toplam ClusterOperator: **34**" in result.answer
    assert "35" not in result.answer
    assert (
        "ai_tool_complete cluster_id=rmtest tool=resources_list "
        "resource=ClusterOperator success=true"
    ) in logs
    assert "config.openshift.io" not in logs


def test_cluster_operator_facts_remain_isolated_by_agent_target() -> None:
    kkb = AgentLoop(
        configured(), FakeLLM([]),
        mcp_with([{"items": [operator(f"kkb-{index}") for index in range(33)]}]),
        "kkbtest", "KKB TEST",
    ).run("ClusterOperator sağlık durumuna bak")
    rm = AgentLoop(
        configured(), FakeLLM([]),
        mcp_with([{"items": [operator(f"rm-{index}") for index in range(34)]}]),
        "rmtest", "RMTEST",
    ).run("ClusterOperator sağlık durumuna bak")
    assert "## KKB TEST" in kkb.answer and "**33**" in kkb.answer
    assert "## RMTEST" in rm.answer and "**34**" in rm.answer
    assert "**34**" not in kkb.answer
    assert "**33**" not in rm.answer


def test_invalid_unknown_argument_never_reaches_mcp() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call(
            "resources_list", '{"apiVersion":"v1","kind":"Pod","group":"x"}'
        )]},
        {"content": "Doğrulanamadı.", "tool_calls": None},
    ])
    mcp = mcp_with([])
    result = AgentLoop(configured(), llm, mcp).run("Pod listesini getir")
    assert mcp.calls == []
    assert result.tool_calls == [{"name": "resources_list", "status": "error"}]


def test_failed_direct_query_stops_without_indirect_tool_safari() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call(
            "resources_list", '{"apiVersion":"config.openshift.io/v1","kind":"ClusterOperator"}'
        )]},
        {"content": None, "tool_calls": [tool_call("events_list")]},
    ])
    mcp = mcp_with([MCPUnavailable("timeout"), {"content": "no events"}])
    result = AgentLoop(configured(), llm, mcp).run(
        "ClusterOperator health durumunu doğrula"
    )
    assert [name for name, _arguments in mcp.calls] == ["resources_list"]
    assert result.answer.startswith("ClusterOperator durumu bu sorguda doğrudan doğrulanamadı")
    assert "RBAC" not in result.answer


def test_generic_health_question_can_still_use_multiple_tools() -> None:
    llm = FakeLLM([{"content": "Genel görünüm stabil; kısmi sinyaller ayrıca belirtilmiştir."}])
    mcp = mcp_with([
        {"items": [operator(f"co-{index}") for index in range(34)]},
        {"nodes": [{"name": "worker-1", "cpu": "100m", "cpuPercent": "5%",
                    "memory": "1Gi", "memoryPercent": "20%"}]},
        {"items": []}, {"items": []},
    ])
    mcp.list_tools = lambda: [
        *RESOURCE_TOOLS,
        *[{
            "name": name,
            "inputSchema": {"type": "object", "properties": {}},
        } for name in ("nodes_top", "pods_list", "events_list")],
    ]
    result = AgentLoop(configured(), llm, mcp).run("cluster genel olarak nasıl")
    assert len(mcp.calls) == 4
    assert result.answer.startswith("## Cluster Genel Sağlık")
    assert "toplam 34" in result.answer
    assert "kısmi" in result.answer
    assert len(llm.calls) == 1


@pytest.mark.parametrize(("cluster_id", "cluster_name"), [
    ("kkbtest", "KKB TEST"), ("rmtest", "RMTEST"),
])
def test_general_health_uses_canonical_cluster_operator_facts(
    cluster_id: str, cluster_name: str,
) -> None:
    llm = FakeLLM([{
        "content": "Toplam: 35 ClusterOperator", "tool_calls": None,
    }])
    mcp = mcp_with([{"items": [operator(f"co-{index}") for index in range(34)]}])
    result = AgentLoop(
        configured(), llm, mcp, cluster_id, cluster_name
    ).run("cluster sağlık durumunu kontrol et")
    assert f"## {cluster_name}" in result.answer
    assert "toplam 34" in result.answer
    assert "35" not in result.answer
    assert result.evidence[0]["facts"]["resource_count"] == 34


def node_metrics_mcp(results):
    mcp = FakeMCP(results)
    mcp.list_tools = lambda: [{
        "name": "nodes_top", "description": "Node CPU and memory metrics",
        "inputSchema": {"type": "object", "properties": {}},
    }]
    return mcp


def test_direct_node_metrics_intent_forces_nodes_top_only() -> None:
    llm = FakeLLM([{
        "content": "worker-1 CPU 120m, memory 1Gi", "tool_calls": None,
    }])
    mcp = node_metrics_mcp([{"nodes": [{
        "name": "worker-1", "cpu": "120m", "cpuPercent": "6%",
        "memory": "1Gi", "memoryPercent": "25%",
    }]}])
    result = AgentLoop(
        configured(), llm, mcp, "rmtest", "RMTEST"
    ).run("node CPU ve memory kullanımı")
    assert mcp.calls == [("nodes_top", {})]
    assert result.evidence == [{"tool": "nodes_top", "status": "success"}]
    assert "| worker-1 | N/A | 120m | 6% | 1Gi | 25% |" in result.answer
    assert llm.calls == []


def test_concise_node_metrics_request_returns_summary_not_table() -> None:
    llm = FakeLLM([])
    mcp = node_metrics_mcp([{"nodes": [
        {"name": "worker-1", "cpu": "100m", "cpuPercent": "5%",
         "memory": "1Gi", "memoryPercent": "20%"},
        {"name": "worker-2", "cpu": "300m", "cpuPercent": "15%",
         "memory": "2Gi", "memoryPercent": "40%"},
    ]}])
    result = AgentLoop(configured(), llm, mcp, "rmtest", "RMTEST").run(
        "node'ların memory kullanımları nasıl, sadece sorumu cevapla"
    )
    assert "memory kullanımı %20-%40 aralığında" in result.answer
    assert "| Node |" not in result.answer
    assert llm.calls == []


def test_direct_node_metrics_failure_does_not_fabricate_values() -> None:
    llm = FakeLLM([])
    result = AgentLoop(
        configured(), llm,
        node_metrics_mcp([MCPUnavailable("metrics unavailable")]),
        "rmtest", "RMTEST",
    ).run("node CPU ve memory kullanımı")
    assert result.answer == "Node CPU ve memory metrikleri bu sorguda doğrulanamadı."
    assert llm.calls == []


def cluster_operator_answer(answer: str) -> str:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call(
            "resources_list",
            '{"apiVersion":"config.openshift.io/v1","kind":"ClusterOperator"}',
        )]},
        {"content": answer, "tool_calls": None},
    ])
    mcp = mcp_with([{"items": [operator(f"co-{index}") for index in range(34)]}])

    return AgentLoop(configured(), llm, mcp).run(
        "ClusterOperator envanterini say"
    ).answer


@pytest.mark.parametrize("answer", [
    "Toplam 35 ClusterOperator",
    "Toplam 35",
    "35 adet ClusterOperator incelendi.",
    "Available 35/35",
    "| Alan | Değer |\n| --- | --- |\n| Total | 35 |",
    "34 ClusterOperator bulundu. Degraded 1.",
    "34 ClusterOperator bulundu. Progressing 2.",
    "34 ClusterOperator bulundu. Unavailable 1.",
])
def test_cluster_operator_numeric_contradiction_uses_deterministic_fallback(
    answer: str,
) -> None:
    result = cluster_operator_answer(answer)

    assert "Toplam ClusterOperator: **34**" in result
    assert "Degraded=True: **0**" in result
    assert "Available=False: **0**" in result
    assert "Progressing=True: **0**" in result


def test_direct_cluster_operator_answer_is_always_backend_rendered() -> None:
    assert cluster_operator_answer("34 ClusterOperator bulundu. Degraded: 0.") == (
        "- Toplam ClusterOperator: **34**\n"
        "- Degraded=True: **0**\n"
        "- Progressing=True: **0**\n"
        "- Available=False: **0**"
    )


@pytest.mark.parametrize("answer", [
    "Available 34/34",
    "34 ClusterOperator bulundu. OpenShift sürümü 4.18.40.",
])
def test_cluster_operator_llm_prose_cannot_override_authoritative_facts(
    answer: str,
) -> None:
    result = cluster_operator_answer(answer)
    assert "Toplam ClusterOperator: **34**" in result
    assert "4.18.40" not in result


def test_cluster_operator_answer_without_authoritative_facts_never_invents_count() -> None:
    expected = "35 ClusterOperator bulundu."
    result = AgentLoop(
        configured(), FakeLLM([{"content": expected, "tool_calls": None}]), mcp_with([])
    ).run("ClusterOperator envanterini say")

    assert result.answer == "ClusterOperator durumu bu sorguda doğrudan doğrulanamadı."
    assert "35" not in result.answer


def test_prompt_forbids_unsupported_rbac_inference() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "rbac" in prompt or "permission" in prompt


def test_prompt_requires_exact_facts_and_forbids_unsupported_severity() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "progressing_true_count" in prompt
    assert "repeat each available deterministic count exactly" in prompt
    assert "temporary" in prompt
    assert "non-critical" in prompt
    assert "unless direct evidence explicitly supports" in prompt
