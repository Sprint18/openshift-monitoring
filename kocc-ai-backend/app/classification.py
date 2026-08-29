from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


ConversationClass = Literal["conversational", "operational"]
ConversationSubtype = Literal["greeting", "identity", "help", "operational"]


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
    operational_tokens = {
        "cluster", "node", "nodes", "pod", "pods", "operator", "degraded",
        "cpu", "memory", "bellek", "egressip", "namespace", "deployment",
        "route", "pvc",
    }
    if token_set & greeting_tokens and not token_set & operational_tokens:
        return ConversationClassification("conversational", "greeting")

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
    return "Merhaba, OpenShift operasyonları hakkında nasıl yardımcı olabilirim?"
