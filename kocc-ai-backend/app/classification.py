from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


ConversationClass = Literal["conversational", "operational"]
ConversationSubtype = Literal[
    "greeting", "identity", "help", "smalltalk", "operational"
]


OPERATIONAL_TOKEN_PREFIXES = (
    "namespace", "proje", "project", "pod", "deploy", "service", "servis",
    "route", "node", "clusteroperator", "operator", "event", "log", "cpu",
    "memory", "bellek", "pvc", "storageclass", "storage", "egressip",
    "resource", "kaynak", "cluster", "degraded", "available",
    "progressing",
)
OPERATIONAL_ASSESSMENT_PREFIXES = (
    "saglik", "health", "status", "durum", "sorun", "problem", "kontrol",
    "incele", "goster", "liste", "kac",
)


@dataclass(frozen=True)
class ConversationClassification:
    conversation_class: ConversationClass
    subtype: ConversationSubtype


def _normalized_tokens(message: str) -> tuple[str, ...]:
    folded = unicodedata.normalize("NFKD", message.casefold().translate(
        str.maketrans({"ı": "i"})
    ))
    ascii_folded = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return tuple(re.findall(r"[a-z0-9]+", ascii_folded))


def classify_conversation(message: str) -> ConversationClassification:
    """Classify only high-confidence conversational families.

    Uncertain input intentionally falls through to operational routing.
    """
    tokens = _normalized_tokens(message)
    token_set = set(tokens)
    if not tokens:
        return ConversationClassification("operational", "operational")
    has_operational_signal = any(
        token.startswith(OPERATIONAL_TOKEN_PREFIXES + OPERATIONAL_ASSESSMENT_PREFIXES)
        for token in tokens
    ) or "co" in token_set

    identity_families = (
        {"sen", "kimsin"},
        {"kimsin"},
        {"who", "are", "you"},
    )
    if any(family <= token_set for family in identity_families):
        return ConversationClassification("conversational", "identity")

    help_families = (
        {"ne", "yapabilirsin"},
        {"nasil", "calisiyorsun"},
        {"what", "can", "you", "do"},
    )
    if "yardim" in token_set or any(
        family <= token_set for family in help_families
    ):
        return ConversationClassification("conversational", "help")

    greeting_tokens = {
        "merhaba", "selam", "hello", "hi", "nasilsin", "tesekkurler",
    }
    if token_set & greeting_tokens and not has_operational_signal:
        return ConversationClassification("conversational", "greeting")

    # Short messages without an operational resource/action/diagnostic signal
    # are safe conversational turns. Longer or uncertain input remains operational.
    if not has_operational_signal and len(tokens) <= 8:
        return ConversationClassification("conversational", "smalltalk")

    return ConversationClassification("operational", "operational")


def conversational_answer(classification: ConversationClassification) -> str | None:
    if classification.conversation_class != "conversational":
        return None
    if classification.subtype == "identity":
        return (
            "Ben KKB ShiftLight AI; KOCC içinde çalışan, read-only OpenShift "
            "operasyon asistanıyım ve multi-cluster kullanım için tasarlandım."
        )
    if classification.subtype == "help":
        return (
            "Ben KKB ShiftLight AI; KOCC içinde çalışan, read-only OpenShift "
            "operasyon asistanıyım. Multi-cluster kaynaklarını seçtiğiniz kapsamda "
            "incelemeye yardımcı olabilirim; erişim KOCC backend tarafından yönetilir."
        )
    if classification.subtype == "smalltalk":
        return "Memnun oldum. OpenShift operasyonları hakkında nasıl yardımcı olabilirim?"
    return "Merhaba, OpenShift operasyonları hakkında nasıl yardımcı olabilirim?"
