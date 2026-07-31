from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

CPU_SUFFIXES = {
    "n": Decimal("0.000001"),
    "u": Decimal("0.001"),
    "m": Decimal("1"),
    "": Decimal("1000"),
}

MEMORY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
    "": 1,
}

QUANTITY_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))([a-zA-Z]*)\s*$"
)


def cpu_to_millicores(value: str | int | float | None) -> int:
    if value is None:
        return 0

    match = QUANTITY_PATTERN.match(str(value))
    if not match:
        return 0

    number_text, suffix = match.groups()
    multiplier = CPU_SUFFIXES.get(suffix)
    if multiplier is None:
        return 0

    try:
        return int(Decimal(number_text) * multiplier)
    except (InvalidOperation, ValueError):
        return 0


def memory_to_bytes(value: str | int | float | None) -> int:
    if value is None:
        return 0

    match = QUANTITY_PATTERN.match(str(value))
    if not match:
        return 0

    number_text, suffix = match.groups()
    multiplier = MEMORY_SUFFIXES.get(suffix)
    if multiplier is None:
        return 0

    try:
        return int(Decimal(number_text) * multiplier)
    except (InvalidOperation, ValueError):
        return 0


def format_cpu(millicores: int | float | None) -> str:
    value = int(millicores or 0)
    if value >= 1000:
        cores = value / 1000
        return f"{cores:.2f}".rstrip("0").rstrip(".") + " Core"
    return f"{value} mCPU"


def format_memory(byte_count: int | float | None) -> str:
    value = float(byte_count or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024

    return "0 B"
