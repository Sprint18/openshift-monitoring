from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Any


logger = logging.getLogger("kocc.ai")


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
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
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
            code = "timeout" if isinstance(
                reason, (TimeoutError, socket.timeout)
            ) else "unavailable"
            logger.warning(
                "ai_backend_request path=%s status=%s exception_type=%s duration_ms=%s",
                path, code, type(exc).__name__,
                round((time.perf_counter() - started) * 1000),
            )
            raise AIBackendError(code) from None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.warning(
                "ai_backend_request path=%s status=invalid_response exception_type=%s duration_ms=%s",
                path, type(exc).__name__,
                round((time.perf_counter() - started) * 1000),
            )
            raise AIBackendError("invalid_response") from None

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
            "evidence": self._tool_metadata(evidence, "tool"),
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
