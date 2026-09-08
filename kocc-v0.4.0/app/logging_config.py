from __future__ import annotations

import logging
import os
import time


SUPPORTED_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def parse_log_level(value: str | None) -> tuple[int, bool]:
    normalized = (value or "INFO").strip().upper()
    return (
        SUPPORTED_LOG_LEVELS.get(normalized, logging.INFO),
        normalized not in SUPPORTED_LOG_LEVELS,
    )


def configure_application_logging(value: str | None = None) -> int:
    level, invalid = parse_log_level(
        os.getenv("KOCC_LOG_LEVEL") if value is None else value
    )
    formatter = UTCFormatter(LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%SZ")
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    logging.getLogger("kocc").setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True
    if invalid:
        logging.getLogger("kocc").warning(
            "logging_config invalid_level=true fallback=INFO"
        )
    return level
