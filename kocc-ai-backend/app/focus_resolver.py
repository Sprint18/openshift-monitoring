from __future__ import annotations

import json
import logging
import re
import time

from app.llm_client import LLMClient, LLMUnavailable


logger = logging.getLogger("kocc_ai.focus")
CLUSTER_IDS = frozenset({"kkbtest", "rmtest"})
MAX_QUESTION_CHARS = 1000
MAX_ANSWER_CHARS = 4000

FOCUS_SYSTEM_PROMPT = """Extract the one Kubernetes namespace selected by the
assistant as the current investigation focus. This is semantic conversation
context, not live cluster evidence. Return JSON only, with exactly this shape:
{"namespace":"example"}. If no single namespace was clearly selected, return
{"namespace":null}. Never return a cluster id, array, nested object, explanation,
Markdown, or additional key."""


def _validated_namespace(content: str) -> tuple[str | None, str]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid_json"
    if not isinstance(payload, dict) or set(payload) != {"namespace"}:
        return None, "invalid_shape"
    value = payload["namespace"]
    if value is None:
        return None, "not_selected"
    if not isinstance(value, str):
        return None, "invalid_type"
    namespace = value.strip().casefold()
    if len(namespace) > 63:
        return None, "overlong"
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
        return None, "malformed"
    if namespace in CLUSTER_IDS:
        return None, "cluster_id"
    return namespace, "validated"


def resolve_semantic_focus(
    llm: LLMClient,
    *,
    question: str,
    answer: str,
    cluster_id: str,
    cluster_name: str,
) -> str | None:
    """Resolve semantic focus in one tools-free, fail-closed LLM request."""
    started = time.perf_counter()
    payload = {
        "question": question.strip()[:MAX_QUESTION_CHARS],
        "assistant_answer": answer.strip()[:MAX_ANSWER_CHARS],
        "active_cluster": {
            "id": cluster_id[:63], "name": cluster_name.strip()[:100],
        },
    }
    try:
        response = llm.chat_completion([
            {"role": "system", "content": FOCUS_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"),
            )},
        ])
        content = response.get("content")
        namespace, reason = _validated_namespace(content)
        logger.info(
            "semantic_focus_resolver status=%s%s reason=%s duration_ms=%s",
            "success" if namespace else "no_focus",
            f" namespace={namespace}" if namespace else "",
            reason, round((time.perf_counter() - started) * 1000),
        )
        return namespace
    except LLMUnavailable:
        logger.warning(
            "semantic_focus_resolver status=unavailable duration_ms=%s",
            round((time.perf_counter() - started) * 1000),
        )
        return None
