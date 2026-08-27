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


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_token: str | None
    llm_model: str
    llm_timeout_seconds: float
    mcp_kkbtest_url: str
    mcp_timeout_seconds: float


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
        mcp_timeout_seconds=positive_float("AI_MCP_TIMEOUT_SECONDS", 10.0),
    )
