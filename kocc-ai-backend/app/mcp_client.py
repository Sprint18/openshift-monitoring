from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any


logger = logging.getLogger("kocc_ai.mcp")
PROTOCOL_VERSION = "2025-03-26"


class MCPUnavailable(RuntimeError):
    pass


def parse_mcp_body(content_type: str, body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8")
    if "text/event-stream" in content_type:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in text.splitlines() if line.startswith("data:")
        ]
        if not data_lines:
            raise MCPUnavailable("MCP returned an empty event stream")
        text = data_lines[-1]
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise MCPUnavailable("MCP returned an invalid response") from None


class MCPClient:
    def __init__(self, endpoint: str, timeout: float) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.session_id: str | None = None
        self._request_id = 0

    def _post(self, payload: dict[str, Any], expect_response: bool = True) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), method="POST",
            headers=headers,
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                body = response.read()
                result = parse_mcp_body(response.headers.get("Content-Type", ""), body)
            logger.info(
                "mcp_request method=%s status=success duration_ms=%s",
                payload.get("method"), round((time.perf_counter() - started) * 1000),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "mcp_request method=%s status=error exception_type=%s duration_ms=%s",
                payload.get("method"), type(exc).__name__,
                round((time.perf_counter() - started) * 1000),
            )
            raise MCPUnavailable("MCP is unavailable") from None
        if expect_response and result.get("error"):
            raise MCPUnavailable("MCP request failed")
        return result

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        response = self._post({
            "jsonrpc": "2.0", "id": self._request_id,
            "method": method, "params": params or {},
        })
        return response.get("result", {})

    def initialize(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "kocc-ai-backend", "version": "0.1.0"},
        })
        self._post({
            "jsonrpc": "2.0", "method": "notifications/initialized",
            "params": {},
        }, expect_response=False)

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._rpc("tools/list")
        return list(result.get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.session_id:
            self.initialize()
        return self._rpc("tools/call", {"name": name, "arguments": arguments})
