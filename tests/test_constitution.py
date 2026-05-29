"""GET / PATCH /v1/identity/constitution."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_identity.app import create_app
from eugene_plexus_identity.settings import Settings


def test_get_constitution_returns_default_name(client: TestClient) -> None:
    response = client.get("/v1/identity/constitution")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Eugene"


def test_patch_constitution_updates_fields(client: TestClient) -> None:
    response = client.patch(
        "/v1/identity/constitution",
        json={
            "name": "Eugene",
            "pronouns": "he/him",
            "coreValues": ["honesty", "intellectual humility"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pronouns"] == "he/him"
    assert body["coreValues"] == ["honesty", "intellectual humility"]

    follow = client.get("/v1/identity/constitution").json()
    assert follow["pronouns"] == "he/him"


def test_patch_constitution_rejects_empty_name(client: TestClient) -> None:
    response = client.patch("/v1/identity/constitution", json={"name": ""})
    # name has minLength=1 in the spec; pydantic should reject before
    # reaching our store. Accept either 400 (our handler) or 422
    # (FastAPI's automatic validation) since both signal "invalid".
    assert response.status_code in (400, 422)


def test_constitution_persists_across_app_reload(settings: Settings, client: TestClient) -> None:
    client.patch(
        "/v1/identity/constitution",
        json={"name": "Eugene", "freeText": "I value patience."},
    )

    fresh_app: FastAPI = create_app(settings=settings)
    with TestClient(fresh_app) as fresh:
        body = fresh.get("/v1/identity/constitution").json()
    assert body["freeText"] == "I value patience."


def test_constitution_get_accepts_service_token(
    authed_client: TestClient, orchestrator_service_token: str
) -> None:
    """The orchestrator reads constitution every chat turn to fold
    into hemisphere prompts — service tokens MUST be able to GET."""
    response = authed_client.get(
        "/v1/identity/constitution",
        headers={"Authorization": f"Bearer {orchestrator_service_token}"},
    )
    assert response.status_code == 200


def test_constitution_patch_rejects_service_token(
    authed_client: TestClient, orchestrator_service_token: str
) -> None:
    """Eugene cannot edit his own constitution. A service token from
    any peer is rejected; only operator-audience tokens may PATCH."""
    response = authed_client.patch(
        "/v1/identity/constitution",
        json={"name": "NotEugene"},
        headers={"Authorization": f"Bearer {orchestrator_service_token}"},
    )
    assert response.status_code == 401


def test_constitution_patch_accepts_operator_token(
    authed_operator_client: TestClient,
) -> None:
    response = authed_operator_client.patch(
        "/v1/identity/constitution",
        json={"name": "Eugene", "pronouns": "they/them"},
    )
    assert response.status_code == 200
