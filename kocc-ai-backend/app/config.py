from __future__ import annotations

import os
from dataclasses import dataclass


def positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_token: str | None
    llm_model: str
    llm_timeout_seconds: float
    mcp_kkbtest_url: str
    mcp_rmtest_url: str
    mcp_timeout_seconds: float
    agent_max_iterations: int = 6
    agent_max_tool_calls: int = 10
    agent_max_tool_result_chars: int = 40000
    agent_max_user_chars: int = 8000
    k8s_timeout_seconds: float = 5.0
    k8s_page_limit: int = 100
    k8s_max_pages: int = 20
    k8s_max_items: int = 5000


def load_settings() -> Settings:
    token = os.getenv("AI_LLM_API_TOKEN")
    return Settings(
        llm_base_url=os.getenv("AI_LLM_BASE_URL", "https://llm.kkb.com.tr").rstrip("/"),
        llm_api_token=token if token else None,
        llm_model=os.getenv("AI_LLM_MODEL", "").strip(),
        llm_timeout_seconds=positive_float("AI_LLM_TIMEOUT_SECONDS", 20.0),
        mcp_kkbtest_url=os.getenv(
            "AI_MCP_KKBTEST_URL", "http://openshift-mcp:8080/mcp"
        ),
        mcp_rmtest_url=os.getenv(
            "AI_MCP_RMTEST_URL",
            "https://mcp.apps.rmocptest1.kkbdomain.com/mcp",
        ),
        mcp_timeout_seconds=positive_float("AI_MCP_TIMEOUT_SECONDS", 10.0),
        agent_max_iterations=bounded_int("AI_AGENT_MAX_ITERATIONS", 6, 1, 10),
        agent_max_tool_calls=positive_int("AI_AGENT_MAX_TOOL_CALLS", 10),
        agent_max_tool_result_chars=positive_int(
            "AI_AGENT_MAX_TOOL_RESULT_CHARS", 40000
        ),
        agent_max_user_chars=positive_int("AI_AGENT_MAX_USER_CHARS", 8000),
        k8s_timeout_seconds=positive_float("AI_K8S_TIMEOUT_SECONDS", 5.0),
        k8s_page_limit=bounded_int("AI_K8S_PAGE_LIMIT", 100, 1, 500),
        k8s_max_pages=bounded_int("AI_K8S_MAX_PAGES", 20, 1, 100),
        k8s_max_items=bounded_int("AI_K8S_MAX_ITEMS", 5000, 1, 20000),
    )
