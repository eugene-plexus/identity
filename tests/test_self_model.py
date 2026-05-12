"""GET /v1/identity/self-model + POST /reflect (501 stub)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_identity.store import IdentityStore


def test_self_model_starts_empty(client: TestClient) -> None:
    response = client.get("/v1/identity/self-model")
    assert response.status_code == 200
    assert response.json() == {"entries": []}


def test_self_model_returns_inserted_entries(client: TestClient, app: FastAPI) -> None:
    """Insert via the store directly (reflection endpoint is 501); the
    read endpoint surfaces what's there."""
    store: IdentityStore = app.state.identity_store
    store.insert_self_model(
        topic="creative-tasks",
        content="I notice my hemispheres disagree more about creative work.",
    )

    response = client.get("/v1/identity/self-model")
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["topic"] == "creative-tasks"


def test_self_model_filters_by_topic(client: TestClient, app: FastAPI) -> None:
    store: IdentityStore = app.state.identity_store
    store.insert_self_model(topic="creative-tasks", content="A")
    store.insert_self_model(topic="analytical-tasks", content="B")
    store.insert_self_model(topic="creative-tasks", content="C")

    response = client.get("/v1/identity/self-model?topic=creative-tasks")
    entries = response.json()["entries"]
    topics = [e["topic"] for e in entries]
    # Topic-exact matches come first; recency-ranked tail may fill in
    # additional rows up to the limit.
    assert topics[0] == "creative-tasks"
    assert all(t in ("creative-tasks", "analytical-tasks") for t in topics)


def test_self_model_respects_limit(client: TestClient, app: FastAPI) -> None:
    store: IdentityStore = app.state.identity_store
    for i in range(10):
        store.insert_self_model(topic=f"topic-{i}", content=f"thought {i}")

    response = client.get("/v1/identity/self-model?limit=3")
    assert response.status_code == 200
    assert len(response.json()["entries"]) == 3


def test_reflect_returns_503_when_hemisphere_unconfigured(client: TestClient) -> None:
    """v0.2 reflection requires `reflectionHemisphereUrl` to be set.
    Without it the endpoint returns 503 — distinguishable from "not
    yet built" (which would be 501) and from "transient outage"."""
    response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 503
    body = response.json()
    assert "reflectionHemisphereUrl" in body["detail"]["detail"]
