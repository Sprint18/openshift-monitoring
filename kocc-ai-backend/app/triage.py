from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from app.conversation import ActiveInspection, SafeTurn, TriageCandidate
from app.llm_client import LLMClient, LLMUnavailable


logger = logging.getLogger("kocc_ai.triage")
CLUSTER_IDS = frozenset({"kkbtest", "rmtest"})
ASSESSMENTS = frozenset({"highest", "tie"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
MAX_CANDIDATES = 10
MAX_RESOURCES = 10
MAX_SELECTED_RESOURCES = 10
MAX_REASONS = 6
MAX_REASON_CHARS = 240
MAX_QUESTION_CHARS = 1000
MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 4000

TRIAGE_SYSTEM_PROMPT = """You are making one evidence-bounded TECHNICAL
OPERATIONAL PRIORITY decision for an OpenShift investigation. Return JSON only
with exactly these keys:
{"assessment":"highest|tie","selected_namespace":"namespace or null",
"tied_namespaces":["namespace"],"selected_resources":["resource"],
"confidence":"low|medium|high","reasons":["bounded reason"]}

Use the same rubric every time:
1. Current failure severity: inability to become Ready/Running, including
CrashLoopBackOff, init failure, Pending, ImagePullBackOff, FailedMount, or
FailedCreate.
2. Observed impact breadth across related resources or nodes.
3. Infrastructure/dependency position such as CSI, storage, ingress, or
monitoring, without inventing business impact.
4. Evidence-bounded failure chains. Mark unproven causality as analysis or a
hypothesis in the reason.
5. Recency of supporting warning evidence when supplied.
6. Evidence strength: direct pod/event observations outrank assumptions.

Do not use namespace naming (lab/test/uat/prod/openshift), object age, raw
cumulative restart count, assumed traffic, assumed business importance,
guessed dependencies, or guessed customer impact as standalone priority proof.
Pod age is not outage duration and restart count is not continuous outage.
Select a namespace only from comparison_candidates. Use assessment=highest for
one defensible technical priority. Use assessment=tie only when evidence cannot
reasonably distinguish candidates; then selected_namespace must be null and at
least two tied_namespaces are required. This is technical priority, not business
criticality. Do not output Markdown, explanations outside JSON, extra keys, or
nested values."""


class TriageInvalid(RuntimeError):
    pass


@dataclass(frozen=True)
class TriageDecision:
    assessment: str
    selected_namespace: str | None
    tied_namespaces: tuple[str, ...]
    selected_resources: tuple[str, ...]
    confidence: str
    reasons: tuple[str, ...]

    def render_answer(self) -> str:
        reasons = "\n".join(f"- {reason}" for reason in self.reasons)
        confidence = {"low": "düşük", "medium": "orta", "high": "yüksek"}[
            self.confidence
        ]
        if self.assessment == "tie":
            names = ", ".join(f"`{item}`" for item in self.tied_namespaces)
            heading = (
                "Yorum: Teknik operasyonel öncelik açısından "
                f"{names} birbirine yakın değerlendirildi."
            )
        else:
            heading = (
                "Yorum: Teknik operasyonel inceleme önceliği olarak "
                f"`{self.selected_namespace}` seçildi."
            )
        return (
            f"{heading}\n\n**Gerekçeler:**\n{reasons}\n\n"
            f"**Güven:** {confidence}. Bu teknik önceliklendirmedir; iş "
            "kritikliği cluster verisinden tek başına belirlenemez."
        )


def candidates_from_inspection(
    inspection: ActiveInspection | None,
) -> tuple[TriageCandidate, ...]:
    if inspection is None:
        return ()
    if inspection.triage_candidates:
        return inspection.triage_candidates[:MAX_CANDIDATES]
    return tuple(
        TriageCandidate(namespace, ())
        for namespace in inspection.problematic_namespaces[:MAX_CANDIDATES]
    )


def _dns_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if (
        not candidate or len(candidate) > 63 or candidate in CLUSTER_IDS
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", candidate)
    ):
        return None
    return candidate


def _resource_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    return (
        candidate
        if candidate and len(candidate) <= 253
        and re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", candidate)
        else None
    )


def _validate_decision(
    content: Any, candidates: tuple[TriageCandidate, ...],
) -> TriageDecision:
    if not isinstance(content, str):
        raise TriageInvalid("invalid_content")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        raise TriageInvalid("invalid_json") from None
    expected = {
        "assessment", "selected_namespace", "tied_namespaces",
        "selected_resources", "confidence", "reasons",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise TriageInvalid("invalid_shape")
    assessment = payload.get("assessment")
    confidence = payload.get("confidence")
    if assessment not in ASSESSMENTS or confidence not in CONFIDENCE_LEVELS:
        raise TriageInvalid("invalid_enum")
    candidate_map = {item.namespace: item for item in candidates}
    selected = (
        None if payload.get("selected_namespace") is None
        else _dns_label(payload.get("selected_namespace"))
    )
    tied_raw = payload.get("tied_namespaces")
    resources_raw = payload.get("selected_resources")
    reasons_raw = payload.get("reasons")
    if not all(isinstance(value, list) for value in (
        tied_raw, resources_raw, reasons_raw,
    )):
        raise TriageInvalid("invalid_lists")
    tied = tuple(dict.fromkeys(
        name for item in tied_raw[:MAX_CANDIDATES]
        if (name := _dns_label(item)) is not None
    ))
    if len(tied) != len(tied_raw) or any(item not in candidate_map for item in tied):
        raise TriageInvalid("invalid_tie_membership")
    if assessment == "highest":
        if selected is None or selected not in candidate_map or tied:
            raise TriageInvalid("invalid_selection")
    elif selected is not None or len(tied) < 2:
        raise TriageInvalid("invalid_tie")
    resources: list[str] = []
    for item in resources_raw:
        name = _resource_name(item)
        if name is None or len(resources) >= MAX_SELECTED_RESOURCES:
            raise TriageInvalid("invalid_resource")
        resources.append(name)
    if selected is not None:
        known_resources = {item.name for item in candidate_map[selected].resources}
        if any(item not in known_resources for item in resources):
            raise TriageInvalid("invalid_resource_membership")
    reasons: list[str] = []
    if not reasons_raw or len(reasons_raw) > MAX_REASONS:
        raise TriageInvalid("invalid_reasons")
    for item in reasons_raw:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > MAX_REASON_CHARS:
            raise TriageInvalid("invalid_reason")
        reasons.append(item.strip())
    return TriageDecision(
        assessment, selected, tied, tuple(resources), confidence, tuple(reasons),
    )


def _bounded_history(turns: Iterable[SafeTurn]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    remaining = MAX_HISTORY_CHARS
    for turn in list(turns)[-MAX_HISTORY_TURNS:]:
        content = turn.content.strip()[:remaining]
        if not content or remaining <= 0:
            continue
        result.append({"role": turn.role, "content": content})
        remaining -= len(content)
    return result


def decide_triage(
    llm: LLMClient,
    *,
    question: str,
    cluster_id: str,
    candidates: tuple[TriageCandidate, ...],
    semantic_history: Iterable[SafeTurn] = (),
) -> TriageDecision:
    bounded_candidates = candidates[:MAX_CANDIDATES]
    if len(bounded_candidates) < 2:
        raise TriageInvalid("insufficient_candidates")
    request = {
        "active_cluster_id": cluster_id,
        "question": question.strip()[:MAX_QUESTION_CHARS],
        "recent_semantic_context": _bounded_history(semantic_history),
        "comparison_candidates": [
            {
                "namespace": item.namespace,
                "resources": [
                    resource.public_dict()
                    for resource in item.resources[:MAX_RESOURCES]
                ],
            }
            for item in bounded_candidates
        ],
    }
    started = time.perf_counter()
    try:
        response = llm.chat_completion([
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                request, ensure_ascii=False, separators=(",", ":"),
            )},
        ])
        decision = _validate_decision(response.get("content"), bounded_candidates)
    except LLMUnavailable:
        logger.warning(
            "triage_decision cluster_id=%s result=llm_unavailable duration_ms=%s",
            cluster_id, round((time.perf_counter() - started) * 1000),
        )
        raise
    except TriageInvalid:
        logger.warning(
            "triage_decision cluster_id=%s result=invalid_response duration_ms=%s",
            cluster_id, round((time.perf_counter() - started) * 1000),
        )
        raise
    if decision.assessment == "tie":
        logger.info(
            "triage_decision cluster_id=%s result=tie candidates=%s confidence=%s duration_ms=%s",
            cluster_id, len(bounded_candidates), decision.confidence,
            round((time.perf_counter() - started) * 1000),
        )
    else:
        logger.info(
            "triage_decision cluster_id=%s selected_namespace=%s confidence=%s candidates=%s duration_ms=%s",
            cluster_id, decision.selected_namespace, decision.confidence,
            len(bounded_candidates), round((time.perf_counter() - started) * 1000),
        )
    return decision
