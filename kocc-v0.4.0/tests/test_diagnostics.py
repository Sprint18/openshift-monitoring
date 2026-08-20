from app.diagnostics import analyze_pod_diagnostics


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
