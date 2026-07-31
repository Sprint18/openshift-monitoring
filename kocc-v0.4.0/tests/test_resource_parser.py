from app.resource_parser import cpu_to_millicores, memory_to_bytes


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
