from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any


logger = logging.getLogger("kocc.patch")


class PatchBackendError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PatchBackendClient:
    READ_PATHS = {
        "summary": "/api/v1/summary",
        "state": "/api/v1/state",
        "events": "/api/v1/events",
        "heartbeats": "/api/v1/heartbeats",
        "acks": "/api/v1/acks",
        "runs": "/api/v1/runs",
        "agents": "/api/v1/agents",
    }

    def __init__(self, base_url: str, timeout_seconds: float, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_token = api_token

    @property
    def configured(self) -> bool:
        return self.base_url.startswith(("http://", "https://"))

    def get(self, resource: str) -> dict[str, Any]:
        try:
            path = self.READ_PATHS[resource]
        except KeyError as exc:
            raise PatchBackendError("invalid_resource") from exc
        return self._request("GET", path)

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/start", payload)

    def stop(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/stop")

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.configured:
            raise PatchBackendError("unavailable")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise PatchBackendError("invalid_response")
            return result
        except urllib.error.HTTPError as exc:
            logger.warning("patch_backend status=http_error http_status=%s", exc.code)
            raise PatchBackendError(f"http_{exc.code}") from None
        except (TimeoutError, socket.timeout):
            logger.warning("patch_backend status=timeout")
            raise PatchBackendError("timeout") from None
        except urllib.error.URLError:
            logger.warning("patch_backend status=unavailable")
            raise PatchBackendError("unavailable") from None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
            logger.warning("patch_backend status=invalid_response")
            raise PatchBackendError("invalid_response") from None
