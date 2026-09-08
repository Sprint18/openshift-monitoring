from __future__ import annotations

import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any


logger = logging.getLogger("kocc.patch")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
QUERY_KEYS = frozenset({
    "limit", "cursor", "cluster", "namespace", "namespace_glob", "search",
    "health", "target_match", "known_tag_only", "classification",
    "version_status", "health_change", "hide_infrastructure",
})


class PatchBackendError(RuntimeError):
    def __init__(self, code: str, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class CentralPatchClient:
    """Allowlisted HTTP adapter for Central Patch Monitor 0.7.2."""

    def __init__(self, base_url: str, timeout_seconds: float, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.api_token = api_token

    @property
    def configured(self) -> bool:
        return self.base_url.startswith(("http://", "https://"))

    def config(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/config")

    def clusters(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/clusters")

    def flows(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/flows")

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/flows/preview", payload)

    def save_design(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/flows/designs", payload)

    def sessions(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/sessions")

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/sessions", payload)

    def session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", self._session_path(session_id))

    def session_action(self, session_id: str, action: str) -> dict[str, Any]:
        if action not in {"baseline", "start", "stop"}:
            raise PatchBackendError("invalid_resource")
        return self._request("POST", f"{self._session_path(session_id)}/{action}")

    def session_view(
        self,
        session_id: str,
        resource: str,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if resource not in {"summary", "images", "targets", "changes", "facets"}:
            raise PatchBackendError("invalid_resource")
        return self._request(
            "GET", f"{self._session_path(session_id)}/{resource}", query=query
        )

    def container(self, session_id: str, row_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self._session_path(session_id)}/containers/{self._identifier(row_id)}",
        )

    def stream(self, session_id: str) -> Iterator[bytes]:
        request = self._build_request(
            "GET", f"{self._session_path(session_id)}/stream", accept="text/event-stream"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                while True:
                    chunk = response.readline()
                    if not chunk:
                        return
                    yield chunk
        except Exception as exc:
            raise self._mapped_error(exc) from None

    @staticmethod
    def filtered_query(values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in values.items()
            if key in QUERY_KEYS and value not in (None, "")
        }

    def _session_path(self, session_id: str) -> str:
        return f"/api/v1/sessions/{self._identifier(session_id)}"

    @staticmethod
    def _identifier(value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise PatchBackendError("invalid_resource")
        return value

    def _build_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> urllib.request.Request:
        if not self.configured or not path.startswith("/api/v1/"):
            raise PatchBackendError("unavailable")
        safe_query = self.filtered_query(query or {})
        url = self.base_url + path
        if safe_query:
            url += "?" + urllib.parse.urlencode(safe_query, doseq=True)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return urllib.request.Request(url, data=body, method=method, headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._build_request(method, path, payload, query)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise PatchBackendError("invalid_response")
            return result
        except PatchBackendError:
            raise
        except Exception as exc:
            raise self._mapped_error(exc) from None

    @staticmethod
    def _mapped_error(exc: Exception) -> PatchBackendError:
        if isinstance(exc, urllib.error.HTTPError):
            logger.warning("patch_backend status=http_error http_status=%s", exc.code)
            return PatchBackendError(f"http_{exc.code}", exc.code)
        if isinstance(exc, (TimeoutError, socket.timeout)):
            logger.warning("patch_backend status=timeout")
            return PatchBackendError("timeout")
        if isinstance(exc, urllib.error.URLError):
            logger.warning("patch_backend status=unavailable")
            return PatchBackendError("unavailable")
        logger.warning("patch_backend status=invalid_response")
        return PatchBackendError("invalid_response")


# Temporary import compatibility for deployments/tests importing the old name.
PatchBackendClient = CentralPatchClient
