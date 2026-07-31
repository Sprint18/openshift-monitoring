from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_unknown_cluster_returns_400() -> None:
    response = client.get("/api/summary?cluster=unknown")

    assert response.status_code == 400


@patch("app.main.new_cluster_client")
def test_unexpected_errors_do_not_leak_details(new_cluster_client: Mock) -> None:
    new_cluster_client.side_effect = RuntimeError("sensitive internal detail")

    response = client.get("/api/summary?cluster=kkbtest")

    assert response.status_code == 500
    assert "sensitive internal detail" not in response.text
