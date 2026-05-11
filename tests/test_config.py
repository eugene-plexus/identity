"""Standard config trio."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_get_config_returns_defaults(client: TestClient) -> None:
    response = client.get("/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["logLevel"] == "INFO"


def test_config_schema_lists_fields(client: TestClient) -> None:
    response = client.get("/v1/config/schema")
    assert response.status_code == 200
    body = response.json()
    keys = {f["key"] for f in body["fields"]}
    assert "logLevel" in keys
    assert body["component"] == "identity"


def test_patch_config_persists(client: TestClient) -> None:
    response = client.patch("/v1/config", json={"logLevel": "WARNING"})
    assert response.status_code == 200
    body = response.json()
    assert "logLevel" in body["applied"]
    assert body["requiresRestart"] is True

    follow = client.get("/v1/config").json()
    assert follow["logLevel"] == "WARNING"


def test_patch_config_rejects_unknown_field(client: TestClient) -> None:
    response = client.patch("/v1/config", json={"bogus": "value"})
    assert response.status_code == 200
    body = response.json()
    assert any(r["key"] == "bogus" for r in body["rejected"])
