from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import Settings


logger = logging.getLogger("kocc_ai.llm")


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url
        self.token = settings.llm_api_token
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise LLMUnavailable("LLM credential is not configured")
        started = time.perf_counter()
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            method="POST" if data is not None else "GET",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            logger.info(
                "llm_request status=success duration_ms=%s",
                round((time.perf_counter() - started) * 1000),
            )
            return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "llm_request status=error exception_type=%s duration_ms=%s",
                type(exc).__name__, round((time.perf_counter() - started) * 1000),
            )
            raise LLMUnavailable("LLM is unavailable") from None

    def check(self) -> bool:
        self._request("/v1/models")
        return True

    def chat(self, message: str) -> str:
        if not self.model:
            raise LLMUnavailable("LLM model is not configured")
        result = self._request("/v1/chat/completions", {
            "model": self.model,
            "messages": [{"role": "user", "content": message}],
        })
        try:
            return str(result["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable("LLM returned an invalid response") from None
