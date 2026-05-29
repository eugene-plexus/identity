"""Pytest fixtures for the identity component.

Two layers:
  * `client` / `app` / `settings` — bare app, auth-disabled (no
    signing key wired in). Default for tests that don't care about
    auth.
  * `authed_client` / `signing_key` / `operator_token` / etc. —
    AuthState pre-populated with a real signing key. Tests that
    assert audience-routing behavior use these.

Helpers for issuing operator and service JWTs live here so individual
test modules don't repeat the PyJWT boilerplate.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_identity.app import create_app
from eugene_plexus_identity.auth_state import AuthState
from eugene_plexus_identity.settings import Settings

_JWT_ALG = "HS256"


def issue_token(
    *,
    signing_key: bytes,
    sub: str,
    aud: str,
    ttl_seconds: int = 3600,
) -> str:
    issued_at = int(time.time())
    claims = {
        "sub": sub,
        "aud": aud,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return jwt.encode(claims, signing_key, algorithm=_JWT_ALG)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_file=tmp_path / "config.yaml",
        constitution_file=tmp_path / "constitution.yaml",
        db_file=tmp_path / "identity.db",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Auth-enabled fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def authed_app(settings: Settings, signing_key: bytes) -> FastAPI:
    app = create_app(settings=settings)
    app.state.auth_state = AuthState(
        signing_key=signing_key,
        service_token=issue_token(
            signing_key=signing_key,
            sub="identity",
            aud="service:identity",
            ttl_seconds=365 * 24 * 3600,
        ),
        master_key=None,
    )
    return app


@pytest.fixture
def authed_client(authed_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(authed_app) as c:
        yield c


@pytest.fixture
def operator_token(signing_key: bytes) -> str:
    return issue_token(signing_key=signing_key, sub="operator", aud="operator")


@pytest.fixture
def orchestrator_service_token(signing_key: bytes) -> str:
    return issue_token(signing_key=signing_key, sub="orchestrator", aud="service:orchestrator")


@pytest.fixture
def connector_service_token(signing_key: bytes) -> str:
    return issue_token(signing_key=signing_key, sub="connector", aud="service:connector")


@pytest.fixture
def authed_operator_client(authed_app: FastAPI, operator_token: str) -> Iterator[TestClient]:
    """Convenience: TestClient with Authorization header pre-attached
    as an operator token. Most CRUD tests want this."""
    with TestClient(authed_app) as c:
        c.headers["Authorization"] = f"Bearer {operator_token}"
        yield c
