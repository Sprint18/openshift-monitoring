from app.diagnostics import analyze_pod_diagnostics, restart_severity


def pod_with_container(**overrides: object) -> dict:
    container = {
        "name": "api",
        "restart_count": 0,
        "waiting_reason": None,
        "terminated_reason": None,
        "last_terminated_reason": None,
        "exit_code": None,
        "last_exit_code": None,
        "memory_limit": None,
    }
    container.update(overrides)
    return {"phase": "Running", "reason": "", "message": "", "containers": [container]}


def test_oomkilled_analyzer() -> None:
    result = analyze_pod_diagnostics(
        pod_with_container(last_terminated_reason="OOMKilled", last_exit_code=137, memory_limit="512Mi"), []
    )
    assert "memory limitini" in result["cause"]
    assert result["severity"] == "critical"
    assert any("512Mi" in item for item in result["evidence"])


def test_image_pull_analyzer() -> None:
    events = [{"reason": "Failed", "message": "pull access denied: unauthorized"}]
    result = analyze_pod_diagnostics(
        pod_with_container(waiting_reason="ImagePullBackOff"), events
    )
    assert "image çekilemiyor" in result["cause"]
    assert any("unauthorized" in item for item in result["evidence"])


def test_crashloop_analyzer() -> None:
    result = analyze_pod_diagnostics(
        pod_with_container(
            waiting_reason="CrashLoopBackOff", restart_count=9,
            last_terminated_reason="Error", last_exit_code=1,
        ), []
    )
    assert "tekrar tekrar" in result["cause"]
    assert any("9" in item for item in result["evidence"])


def test_failed_scheduling_analyzer() -> None:
    events = [{"reason": "FailedScheduling", "message": "0/3 nodes: insufficient cpu"}]
    pod = pod_with_container()
    pod["phase"] = "Pending"
    result = analyze_pod_diagnostics(pod, events)
    assert "schedule edilemiyor" in result["cause"]
    assert "insufficient cpu" in result["evidence"][0]


def test_failed_mount_analyzer() -> None:
    result = analyze_pod_diagnostics(
        pod_with_container(),
        [{"reason": "FailedMount", "message": "secret app-config not found"}],
    )
    assert "mount işlemi" in result["cause"]
    assert "secret app-config" in result["evidence"][0]


def test_probe_failure_analyzer() -> None:
    result = analyze_pod_diagnostics(
        pod_with_container(),
        [{"reason": "Unhealthy", "message": "Readiness probe failed: timeout"}],
    )
    assert "probe başarısız" in result["cause"]
    assert "Readiness" in result["evidence"][0]


def test_unknown_reason_fallback() -> None:
    result = analyze_pod_diagnostics(pod_with_container(), [])
    assert result["cause"] == "Kesin neden belirlenemedi."
    assert result["severity"] == "info"


def test_exit_code_one_repeated_failure_is_sidecar_aware() -> None:
    pod = pod_with_container(
        restart_count=546, terminated_reason="Error", exit_code=1,
        last_terminated_reason="Error", last_exit_code=1,
        last_started_at="2026-08-20T10:00:00Z",
        last_finished_at="2026-08-20T10:00:02Z",
    )
    pod["containers"].append({
        "name": "istio-proxy", "restart_count": 0, "ready": True,
        "waiting_reason": None, "terminated_reason": None,
        "last_terminated_reason": None, "exit_code": None,
        "last_exit_code": None, "memory_limit": None,
    })

    result = analyze_pod_diagnostics(pod, [])

    assert "hata koduyla" in result["cause"]
    assert result["severity"] == "critical"
    assert result["container_findings"][0]["container"] == "api"
    assert all(item["container"] != "istio-proxy" for item in result["container_findings"])
    assert any("code 1" in evidence for evidence in result["evidence"])


def test_restart_severity_thresholds() -> None:
    assert restart_severity(1) == "low"
    assert restart_severity(6) == "warning"
    assert restart_severity(20) == "high"
    assert restart_severity(100) == "critical"


def test_previous_log_signal_is_short_evidence() -> None:
    result = analyze_pod_diagnostics(
        pod_with_container(
            restart_count=9, terminated_reason="Error", exit_code=1,
        ),
        [],
        {"api": {"previous": "startup failed: connection refused to database"}},
    )
    signals = [item for item in result["evidence"] if item.startswith("Log signal")]
    assert signals
    assert "connection refused" in signals[0]
    assert len(signals[0]) < 240
