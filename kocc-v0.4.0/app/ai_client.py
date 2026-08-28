from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any


logger = logging.getLogger("kocc.ai")
CHAT_TRANSPORT_RETRY_DELAY_SECONDS = 0.2


class AIBackendError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AIBackendClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.base_url.startswith(("http://", "https://")):
            raise AIBackendError("unavailable")
        started = time.perf_counter()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        retry_allowed = method == "POST" and path == "/api/v1/chat"
        for attempt in range(1, 3 if retry_allowed else 2):
            request = urllib.request.Request(
                f"{self.base_url}{path}", data=data, method=method,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise AIBackendError("invalid_response")
                logger.info(
                    "ai_backend_request path=%s status=success duration_ms=%s",
                    path, round((time.perf_counter() - started) * 1000),
                )
                return result
            except urllib.error.HTTPError as exc:
                logger.warning(
                    "ai_backend_request path=%s status=http_error http_status=%s duration_ms=%s",
                    path, exc.code, round((time.perf_counter() - started) * 1000),
                )
                raise AIBackendError(f"http_{exc.code}") from None
            except AIBackendError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                logger.warning(
                    "ai_backend_request path=%s status=timeout exception_type=%s duration_ms=%s",
                    path, type(exc).__name__,
                    round((time.perf_counter() - started) * 1000),
                )
                raise AIBackendError("timeout") from None
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    logger.warning(
                        "ai_backend_request path=%s status=timeout exception_type=%s duration_ms=%s",
                        path, type(exc).__name__,
                        round((time.perf_counter() - started) * 1000),
                    )
                    raise AIBackendError("timeout") from None
                if retry_allowed and attempt == 1:
                    logger.warning(
                        "ai_backend_retry path=%s attempt=2 reason=%s",
                        path, type(exc).__name__,
                    )
                    time.sleep(CHAT_TRANSPORT_RETRY_DELAY_SECONDS)
                    continue
                logger.warning(
                    "ai_backend_request path=%s status=unavailable exception_type=%s duration_ms=%s",
                    path, type(exc).__name__,
                    round((time.perf_counter() - started) * 1000),
                )
                raise AIBackendError("unavailable") from None
            except (ConnectionRefusedError, ConnectionResetError, socket.gaierror) as exc:
                if retry_allowed and attempt == 1:
                    logger.warning(
                        "ai_backend_retry path=%s attempt=2 reason=%s",
                        path, type(exc).__name__,
                    )
                    time.sleep(CHAT_TRANSPORT_RETRY_DELAY_SECONDS)
                    continue
                logger.warning(
                    "ai_backend_request path=%s status=unavailable exception_type=%s duration_ms=%s",
                    path, type(exc).__name__,
                    round((time.perf_counter() - started) * 1000),
                )
                raise AIBackendError("unavailable") from None
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                logger.warning(
                    "ai_backend_request path=%s status=invalid_response exception_type=%s duration_ms=%s",
                    path, type(exc).__name__,
                    round((time.perf_counter() - started) * 1000),
                )
                raise AIBackendError("invalid_response") from None
        raise AIBackendError("unavailable")

    def clusters(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/v1/clusters")
        clusters = response.get("clusters")
        if not isinstance(clusters, list):
            raise AIBackendError("invalid_response")
        result = []
        for item in clusters:
            if not isinstance(item, dict):
                raise AIBackendError("invalid_response")
            cluster_id = item.get("id")
            name = item.get("name")
            enabled = item.get("enabled")
            if not isinstance(cluster_id, str) or not isinstance(name, str):
                raise AIBackendError("invalid_response")
            if not isinstance(enabled, bool):
                raise AIBackendError("invalid_response")
            result.append({"id": cluster_id, "name": name, "enabled": enabled})
        return result

    def chat(self, cluster: str, message: str) -> dict[str, Any]:
        response = self._request(
            "POST", "/api/v1/chat", {"cluster": cluster, "message": message}
        )
        answer = response.get("answer")
        tool_calls = response.get("tool_calls", [])
        evidence = response.get("evidence", [])
        if not isinstance(answer, str) or not answer.strip():
            raise AIBackendError("invalid_response")
        if not isinstance(tool_calls, list) or not isinstance(evidence, list):
            raise AIBackendError("invalid_response")
        return {
            "cluster": cluster,
            "answer": answer,
            "tool_calls": self._tool_metadata(tool_calls, "name"),
            "evidence": self._evidence_metadata(evidence),
        }

    @staticmethod
    def _tool_metadata(items: list[Any], name_key: str) -> list[dict[str, str]]:
        result = []
        for item in items:
            if not isinstance(item, dict):
                raise AIBackendError("invalid_response")
            name = item.get(name_key)
            status = item.get("status")
            if not isinstance(name, str) or not isinstance(status, str):
                raise AIBackendError("invalid_response")
            result.append({name_key: name, "status": status})
        return result

    @staticmethod
    def _evidence_metadata(items: list[Any]) -> list[dict[str, Any]]:
        safe_fact_keys = {
            "resource_count", "degraded_true_count", "available_false_count",
            "progressing_true_count",
        }
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise AIBackendError("invalid_response")
            tool = item.get("tool")
            status = item.get("status")
            if not isinstance(tool, str) or not isinstance(status, str):
                raise AIBackendError("invalid_response")
            safe: dict[str, Any] = {"tool": tool, "status": status}
            facts = item.get("facts")
            if isinstance(facts, dict):
                safe_facts = {
                    key: value for key, value in facts.items()
                    if key in safe_fact_keys
                    and isinstance(value, int) and not isinstance(value, bool)
                }
                if safe_facts:
                    safe["facts"] = safe_facts
            result.append(safe)
        return result
