"""Persons CRUD + relationship summary + audience guards."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_identity.store import IdentityStore


def test_list_persons_shows_only_operator_at_startup(client: TestClient) -> None:
    """The identity lifespan calls `ensure_operator()` so the operator
    person exists from turn zero — without it, the orchestrator's
    `_resolve_operator_person_id` returns None on every chat turn,
    falls back to NIL_PERSON_ID, and cross-conversation recall breaks."""
    response = client.get("/v1/identity/persons")
    assert response.status_code == 200
    body = response.json()
    assert len(body["persons"]) == 1
    operator = body["persons"][0]
    assert operator["isOperator"] is True
    assert operator["displayName"] == "Operator"


def test_ensure_operator_is_reentrant_on_subsequent_boots(tmp_path: Path) -> None:
    """Regression for the v0.2 startup-hang bug. The first call to
    `ensure_operator()` on a fresh DB worked fine (no row → falls through
    to `create_person` outside the lock). The SECOND call (an existing
    operator row) found the row INSIDE the lock and then called
    `get_person`, which tried to re-acquire the same non-reentrant
    `threading.Lock`, deadlocking the lifespan forever.

    This test runs the second call on a background thread with a hard
    timeout so a regression hangs the THREAD, not the whole pytest run.
    """
    store = IdentityStore(tmp_path / "identity.db")
    store.load()
    try:
        # First call seeds the operator row.
        first = store.ensure_operator()
        assert first.isOperator is True

        # Second call hits the existing-row path — the one that
        # deadlocked before the lock-release fix.
        result: list = []

        def _call() -> None:
            result.append(store.ensure_operator())

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive(), (
            "ensure_operator() deadlocked on second call — non-reentrant "
            "lock was held while calling get_person()"
        )
        assert len(result) == 1
        assert result[0].personId == first.personId
    finally:
        store.close()


def test_create_person_round_trips(client: TestClient) -> None:
    create = client.post(
        "/v1/identity/persons",
        json={"displayName": "Sarah", "relationshipNote": "my wife"},
    )
    assert create.status_code == 201, create.text
    person = create.json()
    pid = person["personId"]
    assert person["displayName"] == "Sarah"
    assert person["isOperator"] is False
    assert person["relationshipNote"] == "my wife"

    fetched = client.get(f"/v1/identity/persons/{pid}").json()
    assert fetched["personId"] == pid
    assert fetched["displayName"] == "Sarah"


def test_get_unknown_person_returns_404(client: TestClient) -> None:
    response = client.get("/v1/identity/persons/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_patch_person_updates_fields(client: TestClient) -> None:
    pid = client.post(
        "/v1/identity/persons", json={"displayName": "Casual Acquaintance"}
    ).json()["personId"]

    response = client.patch(
        f"/v1/identity/persons/{pid}",
        json={"relationshipNote": "Met at a conference, into ML"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["relationshipNote"] == "Met at a conference, into ML"


def test_delete_person_removes_it(client: TestClient) -> None:
    pid = client.post(
        "/v1/identity/persons", json={"displayName": "Forgettable"}
    ).json()["personId"]
    delete = client.delete(f"/v1/identity/persons/{pid}")
    assert delete.status_code == 204
    assert client.get(f"/v1/identity/persons/{pid}").status_code == 404


def test_cannot_delete_operator(client: TestClient, app: FastAPI) -> None:
    """The store refuses to delete the install's operator record."""
    store: IdentityStore = app.state.identity_store
    operator = store.ensure_operator(display_name="Troy")
    response = client.delete(f"/v1/identity/persons/{operator.personId}")
    assert response.status_code == 409


def test_relationship_summary_minimal_for_v02(client: TestClient) -> None:
    """v0.2 returns a minimal RelationshipSummary — no recent turns,
    no synthesized prose. The memory integration that fills these in
    lands later."""
    pid = client.post(
        "/v1/identity/persons", json={"displayName": "Pal"}
    ).json()["personId"]

    response = client.get(f"/v1/identity/persons/{pid}/relationship")
    assert response.status_code == 200
    body = response.json()
    assert body["personId"] == pid
    assert body["turnCount"] == 0
    assert body["recentTurns"] == []


# --------------------------------------------------------------------------- #
# Audience guards: GET works for any token; mutations operator-only
# --------------------------------------------------------------------------- #


def test_list_persons_accepts_service_token(
    authed_client: TestClient, orchestrator_service_token: str
) -> None:
    """The orchestrator may list persons to render Eugene's known-people
    view at chat assembly time."""
    response = authed_client.get(
        "/v1/identity/persons",
        headers={"Authorization": f"Bearer {orchestrator_service_token}"},
    )
    assert response.status_code == 200


def test_create_person_rejects_service_token(
    authed_client: TestClient, orchestrator_service_token: str
) -> None:
    """Service tokens cannot shape the identity graph — only operators
    add persons (other than via the link-approval flow)."""
    response = authed_client.post(
        "/v1/identity/persons",
        json={"displayName": "Forged"},
        headers={"Authorization": f"Bearer {orchestrator_service_token}"},
    )
    assert response.status_code == 401


def test_delete_person_rejects_service_token(
    authed_operator_client: TestClient,
    orchestrator_service_token: str,
) -> None:
    pid = authed_operator_client.post(
        "/v1/identity/persons", json={"displayName": "Pal"}
    ).json()["personId"]
    response = authed_operator_client.delete(
        f"/v1/identity/persons/{pid}",
        headers={"Authorization": f"Bearer {orchestrator_service_token}"},
    )
    assert response.status_code == 401
