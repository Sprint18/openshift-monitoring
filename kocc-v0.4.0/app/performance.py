from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from typing import Any


logger = logging.getLogger("kocc.performance")
_cluster: ContextVar[str] = ContextVar("perf_cluster", default="unknown")
_path: ContextVar[str] = ContextVar("perf_path", default="unknown")
SLOW_OPERATION_MS = 1000


def set_perf_cluster(cluster: str) -> Token[str]:
    return _cluster.set(cluster)


def reset_perf_cluster(token: Token[str]) -> None:
    _cluster.reset(token)


def set_perf_path(path: str) -> Token[str]:
    return _path.set(path)


def reset_perf_path(token: Token[str]) -> None:
    _path.reset(token)


def get_perf_path() -> str:
    return _path.get()


def elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def log_performance(
    operation: str,
    started_at: float,
    *,
    item_count: int | None = None,
    cache_hit: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    duration = elapsed_ms(started_at)
    fields = [
        f"perf cluster={_cluster.get()}",
        f"path={_path.get()}",
        f"op={operation}",
        f"duration_ms={duration}",
    ]
    if item_count is not None:
        fields.append(f"items={item_count}")
    if cache_hit is not None:
        fields.append(f"cache_hit={str(cache_hit).lower()}")
    if extra:
        fields.extend(f"{key}={value}" for key, value in extra.items())
    logger.info(" ".join(fields))
    if duration > SLOW_OPERATION_MS:
        logger.warning(
            "slow_operation cluster=%s path=%s op=%s duration_ms=%s",
            _cluster.get(), _path.get(), operation, duration,
        )
    return duration
