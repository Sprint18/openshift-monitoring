from __future__ import annotations

from typing import Any


IMAGE_REASONS = {"ImagePullBackOff", "ErrImagePull"}
LOG_SIGNAL_KEYWORDS = (
    "connection refused", "timeout", "permission denied", "no such file",
    "certificate", "authentication", "unauthorized", "out of memory",
    "killed", "exception", "traceback", "panic", "fatal",
    "cannot connect", "failed to start",
)


def restart_severity(restart_count: int) -> str:
    if restart_count >= 100:
        return "critical"
    if restart_count >= 20:
        return "high"
    if restart_count >= 6:
        return "warning"
    return "low"


def extract_log_signals(logs: dict[str, Any] | None) -> list[str]:
    signals: list[str] = []
    for container_name, entries in (logs or {}).items():
        values = entries.values() if isinstance(entries, dict) else [entries]
        for value in values:
            if not isinstance(value, str):
                continue
            for line in value.splitlines():
                lowered = line.lower()
                keyword = next((item for item in LOG_SIGNAL_KEYWORDS if item in lowered), None)
                if keyword:
                    snippet = " ".join(line.strip().split())[:160]
                    evidence = f"Log signal ({container_name}): {keyword} — {snippet}"
                    if evidence not in signals:
                        signals.append(evidence)
                    break
            if len(signals) >= 5:
                return signals
    return signals


def analyze_pod_diagnostics(
    pod: dict[str, Any],
    events: list[dict[str, Any]],
    logs: dict[str, Any] | None = None,
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
    log_signals = extract_log_signals(logs)

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

    failed_containers = []
    for container in containers:
        exit_code = container.get("exit_code")
        last_exit_code = container.get("last_exit_code")
        terminated_reason = container.get("terminated_reason")
        last_reason = container.get("last_terminated_reason")
        restarts = int(container.get("restart_count", 0) or 0)
        nonzero_exit = any(
            value is not None and value != 0 for value in (exit_code, last_exit_code)
        )
        error_termination = terminated_reason == "Error" or last_reason == "Error"
        if restarts and nonzero_exit and error_termination:
            effective_exit = exit_code if exit_code not in (None, 0) else last_exit_code
            evidence = [
                f'Container: {container.get("name", "N/A")}',
                f"Restart count: {restarts}",
                f"Application process exited with code {effective_exit}",
                f'Terminated reason: {terminated_reason or last_reason or "N/A"}',
            ]
            for label, key in (
                ("Started", "last_started_at"),
                ("Finished", "last_finished_at"),
            ):
                if container.get(key):
                    evidence.append(f"{label}: {container[key]}")
            failed_containers.append({
                "container": container.get("name", "N/A"),
                "cause": "Application process exited with a non-zero code",
                "severity": restart_severity(restarts),
                "evidence": evidence,
            })

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
            [f"Restart count: {restarts}", *previous, *log_signals],
            [
                "Previous container logunu inceleyin.",
                "Process exit code ve startup konfigürasyonunu doğrulayın.",
                "Bağımlı servislerin erişilebilirliğini kontrol edin.",
            ],
            "critical",
        )

    if failed_containers:
        highest = max(
            failed_containers,
            key=lambda item: {"low": 1, "warning": 2, "high": 3, "critical": 4}[item["severity"]],
        )
        analysis = result(
            "Uygulama container'ı hata koduyla kapanıp tekrar başlatılıyor.",
            [item for finding in failed_containers for item in finding["evidence"]]
            + log_signals,
            [
                "Previous container loglarını inceleyin.",
                "Application startup/config hatalarını ve environment variable değerlerini doğrulayın.",
                "Secret/ConfigMap içerikleri ile DB/API gibi bağımlı servis erişimini kontrol edin.",
            ],
            highest["severity"],
        )
        analysis["container_findings"] = failed_containers
        return analysis

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
