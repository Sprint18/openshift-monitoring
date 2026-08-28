from __future__ import annotations

from app.agent import AgentLoop, SYSTEM_PROMPT, openai_tools
from app.mcp_client import MCPUnavailable
from app.tool_contracts import validate_tool_arguments
from tests.test_agent import FakeLLM, FakeMCP, configured, tool_call
from tests.test_observations import operator


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
    mcp = mcp_with([{"items": [operator(f"co-{index}") for index in range(34)]}])
    result = AgentLoop(configured(), llm, mcp).run(
        "Degraded ClusterOperator var mı?"
    )
    assert mcp.calls == [("resources_list", {
        "apiVersion": "config.openshift.io/v1", "kind": "ClusterOperator"
    })]
    assert "34 ClusterOperator" in result.answer
    assert "operator.openshift.io" not in result.answer
    assert result.evidence[0]["facts"]["degraded_true_count"] == 0


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
    llm = FakeLLM([
        {"content": None, "tool_calls": [
            tool_call("resources_list", '{"apiVersion":"v1","kind":"Node"}', "one"),
            tool_call("events_list", "{}", "two"),
        ]},
        {"content": "Genel sağlık kanıtları incelendi.", "tool_calls": None},
    ])
    mcp = mcp_with([{"items": []}, {"content": []}])
    result = AgentLoop(configured(), llm, mcp).run("Cluster genel sağlığını incele")
    assert len(mcp.calls) == 2
    assert result.answer == "Genel sağlık kanıtları incelendi."


def test_prompt_forbids_unsupported_rbac_inference() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "rbac" in prompt or "permission" in prompt
