"""Bearer auth contract — same matrix as the orchestrator / hemisphere-
driver / memory components.

Auth-disabled passthrough (no signing key) keeps dev/standalone runs
working. Auth-enabled: missing token → 401; operator audience accepted
broadly; service audience accepted on reads and link-filing only; bad
signature / expired token → 401.
"""

from __future__ import annotations

import secrets
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from eugene_plexus_identity.auth_state import load_auth_state

from .conftest import issue_token


def test_auth_disabled_lets_everything_through(client: TestClient) -> None:
    """No signing key wired in → every route answers normally."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/config").status_code == 200
    assert client.get("/v1/identity/constitution").status_code == 200


def test_missing_bearer_rejects_with_401(authed_client: TestClient) -> None:
    response = authed_client.get("/v1/identity/constitution")
    assert response.status_code == 401
    body = response.json()
    assert body["detail"]["component"] == "identity"


def test_wrong_signing_key_rejects(authed_client: TestClient) -> None:
    other = secrets.token_bytes(32)
    token = issue_token(signing_key=other, sub="operator", aud="operator")
    response = authed_client.get(
        "/v1/identity/constitution",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_expired_token_rejects(
    authed_client: TestClient, signing_key: bytes
) -> None:
    issued_at = int(time.time()) - 120
    expired = jwt.encode(
        {"sub": "operator", "aud": "operator", "iat": issued_at, "exp": issued_at + 60},
        signing_key,
        algorithm="HS256",
    )
    response = authed_client.get(
        "/v1/identity/constitution",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert response.status_code == 401


def test_load_auth_state_disabled_when_no_signing_key() -> None:
    state = load_auth_state(
        signing_key_b64=None, service_token=None, master_key_b64=None
    )
    assert state.auth_disabled is True


def test_load_auth_state_rejects_partial_auth() -> None:
    """SERVICE_TOKEN without AUTH_SIGNING_KEY is a configuration bug."""
    with pytest.raises(ValueError, match="inconsistent"):
        load_auth_state(
            signing_key_b64=None,
            service_token="dummy",
            master_key_b64=None,
        )


def test_load_auth_state_allows_signing_key_without_service_token(
    signing_key: bytes,
) -> None:
    """Identity has no required outbound calls in v0.2 — SERVICE_TOKEN
    optional, just like memory and hemisphere-driver."""
    import base64

    state = load_auth_state(
        signing_key_b64=base64.b64encode(signing_key).decode("ascii"),
        service_token=None,
        master_key_b64=None,
    )
    assert state.signing_key == signing_key
    assert state.auth_disabled is False
