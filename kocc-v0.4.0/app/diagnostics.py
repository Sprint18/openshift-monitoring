from __future__ import annotations

from typing import Any


IMAGE_REASONS = {"ImagePullBackOff", "ErrImagePull"}


def analyze_pod_diagnostics(
    pod: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Produce conservative, evidence-based diagnostic guidance."""
    containers = pod.get("containers", [])
    reasons = {
        value
        for container in containers
        for value in (
            container.get("waiting_reason"),
            container.get("terminated_reason"),
            container.get("last_terminated_reason"),
        )
        if value
    }
    exit_codes = {
        value
        for container in containers
        for value in (
            container.get("exit_code"),
            container.get("last_exit_code"),
        )
        if value is not None
    }
    event_reasons = {event.get("reason", "") for event in events}
    event_messages = [event.get("message", "") for event in events]

    def result(
        cause: str,
        evidence: list[str],
        checks: list[str],
        severity: str = "warning",
    ) -> dict[str, Any]:
        return {
            "cause": cause,
            "evidence": [item for item in evidence if item],
            "checks": checks,
            "severity": severity,
        }

    if "OOMKilled" in reasons or 137 in exit_codes:
        limits = [
            f'{item["name"]}: {item["memory_limit"]}'
            for item in containers
            if item.get("memory_limit")
        ]
        return result(
            "Container memory limitini aşmış olabilir.",
            ["OOMKilled veya exit code 137 gözlendi.", *limits],
            [
                "Memory usage trendini inceleyin.",
                "Memory request/limit değerlerini doğrulayın.",
                "Memory leak ve JVM/container heap ayarlarını kontrol edin.",
            ],
            "critical",
        )

    image_reason = next(iter(sorted(IMAGE_REASONS & reasons)), None)
    if image_reason:
        image_evidence = [image_reason]
        image_evidence.extend(
            message for message in event_messages
            if any(token in message.lower() for token in (
                "manifest unknown", "unauthorized", "pull access denied",
                "timeout", "no such host", "certificate",
            ))
        )
        return result(
            "Container image çekilemiyor olabilir.",
            image_evidence,
            [
                "Image adı ve tag/digest değerini doğrulayın.",
                "Registry yetkisi ve imagePullSecret ayarını kontrol edin.",
                "Registry DNS, ağ ve sertifika erişimini test edin.",
            ],
            "critical",
        )

    if "CrashLoopBackOff" in reasons:
        restarts = sum(item.get("restart_count", 0) for item in containers)
        previous = [
            f'{item["name"]}: {item.get("last_terminated_reason") or "N/A"}, '
            f'exit {item.get("last_exit_code")}'
            for item in containers
            if item.get("restart_count", 0)
        ]
        return result(
            "Container tekrar tekrar başlayıp kapanıyor olabilir.",
            [f"Restart count: {restarts}", *previous],
            [
                "Previous container logunu inceleyin.",
                "Process exit code ve startup konfigürasyonunu doğrulayın.",
                "Bağımlı servislerin erişilebilirliğini kontrol edin.",
            ],
            "critical",
        )

    if (
        "FailedScheduling" in event_reasons
        or (
            pod.get("phase") == "Pending"
            and not event_reasons.intersection(
                {"FailedMount", "FailedAttachVolume", "Unhealthy"}
            )
        )
    ):
        scheduling_messages = [
            message for event, message in zip(events, event_messages)
            if event.get("reason") == "FailedScheduling"
        ]
        return result(
            "Pod uygun bir node üzerine schedule edilemiyor olabilir.",
            scheduling_messages or ["Pod Pending durumda."],
            [
                "Insufficient CPU/memory mesajlarını kontrol edin.",
                "Node selector/affinity ve taint/toleration ayarlarını inceleyin.",
                "PVC binding durumunu doğrulayın.",
            ],
        )

    if "FailedMount" in event_reasons or "FailedAttachVolume" in event_reasons:
        return result(
            "Volume, PVC, Secret veya ConfigMap mount işlemi başarısız olabilir.",
            [
                event.get("message", "") for event in events
                if event.get("reason") in {"FailedMount", "FailedAttachVolume"}
            ],
            [
                "İlgili PVC/volume durumunu kontrol edin.",
                "Secret veya ConfigMap kaynağının mevcut olduğunu doğrulayın.",
                "Storage attach/mount eventlerini inceleyin.",
            ],
        )

    if "Unhealthy" in event_reasons:
        return result(
            "Readiness veya liveness probe başarısız olabilir.",
            [
                event.get("message", "") for event in events
                if event.get("reason") == "Unhealthy"
            ],
            [
                "Probe path, port, timeout ve failureThreshold değerlerini kontrol edin.",
                "Uygulama startup ve response süresini inceleyin.",
            ],
        )

    if pod.get("reason") == "Evicted" or "Evicted" in reasons:
        return result(
            "Pod node pressure nedeniyle evicted edilmiş olabilir.",
            [pod.get("message", "Evicted")],
            [
                "Node memory/disk/PID pressure koşullarını kontrol edin.",
                "Pod request/limit ve ephemeral storage kullanımını inceleyin.",
            ],
            "critical",
        )

    return result(
        "Kesin neden belirlenemedi.",
        ["Bilinen tanı kurallarıyla eşleşen yeterli kanıt bulunamadı."],
        ["Pod events ve container loglarını inceleyin."],
        "info",
    )
