from app.resource_parser import (
    cpu_to_millicores,
    format_cpu,
    format_memory,
    memory_to_bytes,
)


def test_cpu_quantity_supports_standard_and_exponent_formats() -> None:
    assert cpu_to_millicores("250m") == 250
    assert cpu_to_millicores("2") == 2000
    assert cpu_to_millicores("1e3m") == 1000


def test_memory_quantity_supports_binary_and_exponent_formats() -> None:
    assert memory_to_bytes("1Gi") == 1024**3
    assert memory_to_bytes("129e6") == 129_000_000


def test_invalid_quantities_return_zero() -> None:
    assert cpu_to_millicores("not-a-quantity") == 0
    assert memory_to_bytes(None) == 0


def test_cpu_formatter_uses_consistent_units() -> None:
    assert format_cpu(10) == "10 mCPU"
    assert format_cpu(280) == "280 mCPU"
    assert format_cpu(1000) == "1 Core"
    assert format_cpu(1220) == "1.22 Core"
    assert format_cpu(5890) == "5.89 Core"


def test_memory_formatter_uses_binary_units_without_trailing_zeroes() -> None:
    assert format_memory(0) == "0 B"
    assert format_memory(512 * 1024**2) == "512 MiB"
    assert format_memory(1024**3) == "1 GiB"
    assert format_memory(int(4.9 * 1024**3)) == "4.9 GiB"
    assert format_memory(int(21.75 * 1024**3)) == "21.75 GiB"
