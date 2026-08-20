import logging

from app import performance


def test_performance_log_contains_structured_fields(monkeypatch, caplog) -> None:
    monkeypatch.setattr(performance.time, "perf_counter", lambda: 2.5)
    token = performance.set_perf_cluster("kkbtest")
    try:
        with caplog.at_level(logging.INFO, logger="kocc.performance"):
            performance.log_performance(
                "api.list_pods", 2.0, item_count=955, cache_hit=False
            )
    finally:
        performance.reset_perf_cluster(token)

    assert "cluster=kkbtest" in caplog.text
    assert "op=api.list_pods" in caplog.text
    assert "duration_ms=500" in caplog.text
    assert "items=955" in caplog.text
    assert "cache_hit=false" in caplog.text


def test_slow_operation_threshold(monkeypatch, caplog) -> None:
    monkeypatch.setattr(performance.time, "perf_counter", lambda: 3.01)
    with caplog.at_level(logging.INFO, logger="kocc.performance"):
        performance.log_performance("api.list_nodes", 2.0)
    assert "slow_operation cluster=unknown path=unknown op=api.list_nodes duration_ms=1010" in caplog.text
