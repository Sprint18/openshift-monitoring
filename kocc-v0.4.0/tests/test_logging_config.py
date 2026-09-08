from __future__ import annotations

import logging
import re
from pathlib import Path

from app import main
from app.logging_config import (
    UTCFormatter,
    configure_application_logging,
    parse_log_level,
)


def test_log_level_parsing_defaults_and_is_case_insensitive() -> None:
    assert parse_log_level(None) == (logging.INFO, False)
    assert parse_log_level("debug") == (logging.DEBUG, False)
    assert parse_log_level(" INFO ") == (logging.INFO, False)
    assert parse_log_level("warning") == (logging.WARNING, False)
    assert parse_log_level("ERROR") == (logging.ERROR, False)
    assert parse_log_level("verbose") == (logging.INFO, True)


def test_application_log_timestamp_is_explicit_utc() -> None:
    formatter = UTCFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    record = logging.LogRecord("kocc", logging.INFO, __file__, 1, "started", (), None)
    rendered = formatter.format(record)
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO kocc started", rendered)


def test_invalid_log_level_warns_once_and_falls_back_to_info(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="kocc"):
        assert configure_application_logging("unsupported") == logging.INFO
    assert caplog.text.count("logging_config invalid_level=true fallback=INFO") == 1


def test_successful_probe_and_static_requests_are_debug_only() -> None:
    assert main.request_log_level("/health", 200) == logging.DEBUG
    assert main.request_log_level("/ready", 200) == logging.DEBUG
    assert main.request_log_level("/favicon.ico", 304) == logging.DEBUG
    assert main.request_log_level("/static/app.css", 200) == logging.DEBUG
    assert main.request_log_level("/api/summary", 200) == logging.INFO
    assert main.request_log_level("/health", 500) == logging.ERROR


def test_patch_401_request_logging_is_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(main, "LOG_LEVEL", logging.INFO)
    main._request_log_last.clear()
    path = "/api/patch/summary"
    assert main.should_log_request(path, "kkbtest", 401, now=100.0) is True
    assert main.should_log_request(path, "kkbtest", 401, now=101.0) is False
    assert main.should_log_request(path, "kkbtest", 401, now=161.0) is True
    monkeypatch.setattr(main, "LOG_LEVEL", logging.DEBUG)
    assert main.should_log_request(path, "kkbtest", 401, now=162.0) is True


def test_request_log_contract_excludes_sensitive_request_data() -> None:
    source = Path(main.__file__).read_text()
    request_log = source[source.index('"http_request method='):]
    request_log = request_log[:request_log.index("log_performance(")]
    assert "method=%s" in request_log
    assert "path=%s" in request_log
    assert "status=%s" in request_log
    assert "duration_ms=%s" in request_log
    assert "cluster=%s" in request_log
    for sensitive in ("Authorization", "password", "cookie", "session"):
        assert sensitive not in request_log
