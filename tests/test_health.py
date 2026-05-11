"""GET /healthz."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["component"] == "identity"
    assert body["safeMode"] is False


def test_healthz_is_open_when_auth_enabled(authed_client: TestClient) -> None:
    """Even with auth enabled, /healthz must not require a token —
    supervisors and load balancers probe it without credentials."""
    response = authed_client.get("/healthz")
    assert response.status_code == 200
