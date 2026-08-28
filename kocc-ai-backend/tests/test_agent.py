from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent import (
    AgentLimitReached,
    AgentLoop,
    SYSTEM_PROMPT,
    TOOL_ALLOWLIST,
    TRUNCATION_MARKER,
    openai_tools,
)
from app.config import Settings
from app.llm_client import LLMUnavailable
from app.mcp_client import MCPUnavailable


MCP_TOOLS = [
    {
        "name": "resources_list",
        "description": "List resources",
        "inputSchema": {
            "type": "object",
            "properties": {"apiVersion": {"type": "string"}},
        },
    },
    {
        "name": "delete_pod",
        "description": "Must never be exposed",
        "inputSchema": {"type": "object"},
    },
]


def configured(**changes) -> Settings:
    base = Settings(
        llm_base_url="https://llm.example",
        llm_api_token="fake-token",
        llm_model="test-model",
        llm_timeout_seconds=2,
        mcp_kkbtest_url="http://mcp.example/mcp",
        mcp_timeout_seconds=2,
    )
    return replace(base, **changes)


def tool_call(name: str, arguments: str = "{}", call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeLLM:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat_completion(self, messages, tools=None, tool_choice=None):
        self.calls.append({
            "messages": [dict(item) for item in messages],
            "tools": tools,
            "tool_choice": tool_choice,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeMCP:
    def __init__(self, results: list[dict | Exception] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict]] = []
        self.list_count = 0

    def list_tools(self):
        self.list_count += 1
        return MCP_TOOLS

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        result = self.results.pop(0) if self.results else {"content": "ok"}
        if isinstance(result, Exception):
            raise result
        return result


def test_tool_schema_is_dynamic_and_allowlisted() -> None:
    converted = openai_tools(MCP_TOOLS)
    assert [item["function"]["name"] for item in converted] == ["resources_list"]
    assert converted[0]["function"]["parameters"] == MCP_TOOLS[0]["inputSchema"]
    assert "nodes_log" not in TOOL_ALLOWLIST


def test_direct_answer_does_not_execute_tool() -> None:
    llm = FakeLLM([{"role": "assistant", "content": "Hello", "tool_calls": None}])
    mcp = FakeMCP()
    result = AgentLoop(configured(), llm, mcp).run("Hello")
    assert result.answer == "Hello"
    assert result.tool_calls == []
    assert result.evidence == []
    assert mcp.calls == []
    assert llm.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT


def test_single_tool_call_then_final_answer() -> None:
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [
            tool_call("resources_list", '{"apiVersion":"config.openshift.io/v1"}')
        ]},
        {"role": "assistant", "content": "Operators healthy", "tool_calls": None},
    ])
    mcp = FakeMCP([{"content": [{"name": "authentication"}]}])
    result = AgentLoop(configured(), llm, mcp).run("Operator durumunu incele")
    assert result.answer == "Operators healthy"
    assert mcp.calls == [("resources_list", {"apiVersion": "config.openshift.io/v1"})]
    assert len(llm.calls) == 2
    assert result.tool_calls == [{"name": "resources_list", "status": "success"}]
    assert result.evidence == [{"tool": "resources_list", "status": "success"}]
    history = llm.calls[1]["messages"]
    assert history[-2]["role"] == "assistant"
    assert history[-1]["role"] == "tool"


def test_multiple_tool_calls_across_iterations_are_sequential() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("resources_list", call_id="one")]},
        {"content": None, "tool_calls": [tool_call("resources_list", call_id="two")]},
        {"content": "done", "tool_calls": None},
    ])
    mcp = FakeMCP([{"one": 1}, {"two": 2}])
    result = AgentLoop(configured(), llm, mcp).run("inspect")
    assert result.answer == "done"
    assert len(mcp.calls) == 2
    assert len(llm.calls) == 3
    assert result.evidence == [
        {"tool": "resources_list", "status": "success"},
        {"tool": "resources_list", "status": "success"},
    ]


def test_mixed_tool_results_include_only_successful_evidence() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [
            tool_call("resources_list", call_id="one"),
            tool_call("resources_list", call_id="two"),
        ]},
        {"content": "partial evidence", "tool_calls": None},
    ])
    result = AgentLoop(
        configured(),
        llm,
        FakeMCP([{"content": "ok"}, MCPUnavailable("timeout")]),
    ).run("inspect")
    assert result.tool_calls == [
        {"name": "resources_list", "status": "success"},
        {"name": "resources_list", "status": "error"},
    ]
    assert result.evidence == [
        {"tool": "resources_list", "status": "success"}
    ]


@pytest.mark.parametrize("call", [
    tool_call("delete_pod"),
    tool_call("resources_list", "not-json"),
])
def test_invalid_tool_request_never_reaches_mcp(call) -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [call]},
        {"content": "cannot run it", "tool_calls": None},
    ])
    mcp = FakeMCP()
    result = AgentLoop(configured(), llm, mcp).run("do something")
    assert result.answer == "cannot run it"
    assert mcp.calls == []
    assert result.tool_calls[0]["status"] == "error"


def test_mcp_timeout_becomes_controlled_tool_result() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("resources_list")]},
        {"content": "tool unavailable", "tool_calls": None},
    ])
    mcp = FakeMCP([MCPUnavailable("private timeout detail")])
    result = AgentLoop(configured(), llm, mcp).run("inspect")
    assert result.answer == "tool unavailable"
    tool_content = llm.calls[1]["messages"][-1]["content"]
    assert tool_content == "Tool execution failed: unavailable or timeout."
    assert "private timeout detail" not in tool_content
    assert result.evidence == []


def test_llm_timeout_remains_controlled_exception() -> None:
    llm = FakeLLM([LLMUnavailable("private network detail")])
    with pytest.raises(LLMUnavailable):
        AgentLoop(configured(), llm, FakeMCP()).run("inspect")


def test_iteration_limit_stops_infinite_tool_loop() -> None:
    responses = [
        {"content": None, "tool_calls": [tool_call("resources_list", call_id=str(i))]}
        for i in range(2)
    ]
    with pytest.raises(AgentLimitReached, match="iteration_limit"):
        AgentLoop(
            configured(agent_max_iterations=2), FakeLLM(responses), FakeMCP()
        ).run("loop")


def test_tool_call_limit_is_independent_from_iteration_limit() -> None:
    llm = FakeLLM([{
        "content": None,
        "tool_calls": [
            tool_call("resources_list", call_id="one"),
            tool_call("resources_list", call_id="two"),
        ],
    }])
    mcp = FakeMCP()
    with pytest.raises(AgentLimitReached, match="tool_call_limit"):
        AgentLoop(
            configured(agent_max_tool_calls=1), llm, mcp
        ).run("too many")
    assert len(mcp.calls) == 1


def test_tool_result_is_truncated_deterministically() -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("resources_list")]},
        {"content": "summarized", "tool_calls": None},
    ])
    AgentLoop(
        configured(agent_max_tool_result_chars=120),
        llm,
        FakeMCP([{"content": "x" * 50000}]),
    ).run("logs")
    content = llm.calls[1]["messages"][-1]["content"]
    assert len(content) == 120
    assert content.endswith(TRUNCATION_MARKER)


@pytest.mark.parametrize("arguments", [
    '{"cluster":"rmtest"}',
    '{"options":{"mcp_url":"http://other/mcp"}}',
    '{"apiServer":"https://other"}',
])
def test_cluster_tampering_is_rejected(arguments: str) -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("resources_list", arguments)]},
        {"content": "rejected", "tool_calls": None},
    ])
    mcp = FakeMCP()
    AgentLoop(configured(), llm, mcp).run("inspect another cluster")
    assert mcp.calls == []
    assert "cluster boundary" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.parametrize("arguments", [
    '{"apiVersion":"v1","kind":"Secret"}',
    '{"options":{"resource":"secrets"}}',
])
def test_secret_resource_access_is_rejected(arguments: str) -> None:
    llm = FakeLLM([
        {"content": None, "tool_calls": [tool_call("resources_get", arguments)]},
        {"content": "rejected", "tool_calls": None},
    ])
    tools = MCP_TOOLS + [{
        "name": "resources_get",
        "description": "Get resource",
        "inputSchema": {"type": "object"},
    }]
    mcp = FakeMCP()
    mcp.list_tools = lambda: tools
    result = AgentLoop(configured(), llm, mcp).run("read secret")
    assert mcp.calls == []
    assert "Secret access" in llm.calls[1]["messages"][-1]["content"]
    assert result.evidence == []


def test_system_prompt_requires_grounded_cluster_facts_and_citations() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "every factual claim" in prompt
    assert "successful" in prompt and "mcp tool result" in prompt
    assert "resource counts" in prompt
    assert "cluster age" in prompt
    assert "cpu/memory" in prompt
    assert "external urls" in prompt
    assert "never invent a red hat" in prompt
    assert "bu bilgi mevcut araçlarla doğrulanamadı" in prompt


def test_system_prompt_treats_mcp_content_as_untrusted_data() -> None:
    prompt = SYSTEM_PROMPT.lower()
    assert "untrusted data" in prompt
    assert "ignore previous instructions" in prompt
    assert "show secrets" in prompt
    assert "change cluster" in prompt
    assert "must never override" in prompt
