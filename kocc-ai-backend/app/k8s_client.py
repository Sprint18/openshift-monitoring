from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.k8s_policy import validate_k8s_request


logger = logging.getLogger("kocc_ai.k8s_api")
IN_CLUSTER_API_URL = "https://kubernetes.default.svc"
SERVICEACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SERVICEACCOUNT_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


@dataclass(frozen=True)
class KubernetesListResult:
    items: tuple[dict[str, Any], ...]
    completeness: str
    pages: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class KubernetesGetResult:
    item: dict[str, Any] | None
    completeness: str
    warnings: tuple[str, ...] = ()


class KubernetesAPIAdapter:
    def __init__(
        self,
        *,
        cluster_id: str,
        timeout_seconds: float,
        page_limit: int,
        max_pages: int,
        max_items: int,
        base_url: str = IN_CLUSTER_API_URL,
        token_path: str = SERVICEACCOUNT_TOKEN_PATH,
        ca_path: str = SERVICEACCOUNT_CA_PATH,
    ) -> None:
        self.cluster_id = cluster_id
        self.timeout_seconds = timeout_seconds
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.max_items = max_items
        self._base_url = base_url.rstrip("/")
        self._token_path = token_path
        self._ca_path = ca_path

    def _request(self, path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
        token = Path(self._token_path).read_text(encoding="utf-8").strip()
        if not token:
            raise OSError("ServiceAccount token is empty")
        url = f"{self._base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )
        context = ssl.create_default_context(cafile=self._ca_path)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=context
            ) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None

    @staticmethod
    def _project_namespace(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        metadata = item.get("metadata")
        status = item.get("status")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name:
            return None
        projected: dict[str, Any] = {"metadata": {"name": name}}
        phase = status.get("phase") if isinstance(status, dict) else None
        if isinstance(phase, str) and phase:
            projected["status"] = {"phase": phase}
        return projected

    def k8s_list(
        self,
        *,
        api_version: str,
        kind: str,
        namespace: str | None = None,
        label_selector: str | None = None,
        field_selector: str | None = None,
        limit: int | None = None,
    ) -> KubernetesListResult:
        validate_k8s_request(
            operation="list", api_version=api_version, kind=kind,
            namespace=namespace,
        )
        requested_limit = min(max(limit or self.page_limit, 1), self.page_limit)
        started = time.perf_counter()
        items: list[dict[str, Any]] = []
        continuation = ""
        pages = 0
        completeness = "complete"
        warnings: list[str] = []
        try:
            while True:
                if pages >= self.max_pages:
                    completeness = "partial"; warnings.append("max_pages_reached"); break
                query = {"limit": str(requested_limit)}
                if continuation:
                    query["continue"] = continuation
                if label_selector:
                    query["labelSelector"] = label_selector
                if field_selector:
                    query["fieldSelector"] = field_selector
                status_code, payload = self._request("/api/v1/namespaces", query)
                if status_code != 200 or not isinstance(payload, dict):
                    completeness = "partial" if items else "unavailable"
                    warnings.append(f"http_{status_code}")
                    break
                pages += 1
                page_items = payload.get("items")
                metadata = payload.get("metadata")
                if not isinstance(page_items, list) or not isinstance(metadata, dict):
                    completeness = "partial" if items else "unavailable"
                    warnings.append("invalid_response")
                    break
                page_overflow = False
                for item in page_items:
                    projected = self._project_namespace(item)
                    if projected is not None:
                        if len(items) >= self.max_items:
                            page_overflow = True
                            break
                        items.append(projected)
                continuation_value = metadata.get("continue", "")
                if not isinstance(continuation_value, str):
                    completeness = "partial"; warnings.append("invalid_continue"); break
                if page_overflow or (
                    len(items) >= self.max_items and continuation_value
                ):
                    completeness = "partial"
                    warnings.append("max_items_reached")
                    break
                continuation = continuation_value
                if not continuation:
                    break
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            completeness = "partial" if items else "unavailable"
            warnings.append("request_failed")
        logger.info(
            "k8s_api_request cluster_id=%s operation=list resource=Namespace status=%s pages=%s items=%s duration_ms=%s",
            self.cluster_id, completeness, pages, len(items),
            round((time.perf_counter() - started) * 1000),
        )
        return KubernetesListResult(
            tuple(items), completeness, pages, tuple(dict.fromkeys(warnings))
        )

    def k8s_get(
        self,
        *,
        api_version: str,
        kind: str,
        name: str,
        namespace: str | None = None,
    ) -> KubernetesGetResult:
        validate_k8s_request(
            operation="get", api_version=api_version, kind=kind,
            namespace=namespace, name=name,
        )
        started = time.perf_counter()
        completeness, warnings, item = "complete", [], None
        try:
            status_code, payload = self._request(
                f"/api/v1/namespaces/{urllib.parse.quote(name, safe='')}"
            )
            if status_code == 200:
                item = self._project_namespace(payload)
                if item is None:
                    completeness = "unavailable"; warnings.append("invalid_response")
            elif status_code == 404:
                item = None
            else:
                completeness = "unavailable"; warnings.append(f"http_{status_code}")
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            completeness = "unavailable"; warnings.append("request_failed")
        logger.info(
            "k8s_api_request cluster_id=%s operation=get resource=Namespace status=%s pages=1 items=%s duration_ms=%s",
            self.cluster_id, completeness, int(item is not None),
            round((time.perf_counter() - started) * 1000),
        )
        return KubernetesGetResult(item, completeness, tuple(warnings))
