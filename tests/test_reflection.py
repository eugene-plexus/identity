"""Reflection: prompt assembly, response parsing, persistence, 503 paths."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_identity.clients.hemisphere_client import HemisphereError
from eugene_plexus_identity.clients.memory_client import MemoryMessage
from eugene_plexus_identity.reflection import (
    build_reflection_user_message,
    parse_reflection_response,
)

# ---------------------------------------------------------------------------
# parse_reflection_response — pure-function tests
# ---------------------------------------------------------------------------


def test_parse_clean_json_object() -> None:
    text = (
        '{"entries":[{"topic":"creative-tasks","content":"I notice I '
        'tend to ramble when the topic is open-ended."}]}'
    )
    parsed = parse_reflection_response(text)
    assert parsed == [("creative-tasks", "I notice I tend to ramble when the topic is open-ended.")]


def test_parse_json_inside_markdown_code_fence() -> None:
    text = (
        "Here's the reflection:\n"
        "```json\n"
        '{"entries":[{"topic":"uncertainty","content":"I hedge more than I should."}]}\n'
        "```\n"
        "Hope that helps."
    )
    parsed = parse_reflection_response(text)
    assert parsed == [("uncertainty", "I hedge more than I should.")]


def test_parse_json_with_surrounding_prose() -> None:
    """Some models prepend or append explanation prose despite the
    "JSON only" instruction. The parser falls back to first-{ to
    last-} extraction."""
    text = (
        "Sure, here are my observations: "
        '{"entries":[{"topic":"a","content":"A observation"},'
        '{"topic":"b","content":"B observation"}]} '
        "Let me know if you want more."
    )
    parsed = parse_reflection_response(text)
    assert parsed == [("a", "A observation"), ("b", "B observation")]


def test_parse_empty_entries_list_returns_empty() -> None:
    text = '{"entries": []}'
    assert parse_reflection_response(text) == []


def test_parse_malformed_json_returns_empty_not_raises() -> None:
    """The route must never 5xx on a malformed response — a bad reflection
    just produces zero new entries, which the operator can re-trigger."""
    assert parse_reflection_response("not json at all") == []
    assert parse_reflection_response("") == []
    assert parse_reflection_response("{ unterminated") == []


def test_parse_skips_non_string_topic_or_content() -> None:
    text = (
        '{"entries": ['
        '{"topic": "ok", "content": "valid"},'
        '{"topic": null, "content": "topic-missing"},'
        '{"topic": "bad", "content": 42},'
        '{"topic": "  ", "content": "blank topic"}'
        "]}"
    )
    parsed = parse_reflection_response(text)
    assert parsed == [("ok", "valid")]


# ---------------------------------------------------------------------------
# build_reflection_user_message — pure-function tests
# ---------------------------------------------------------------------------


def test_build_user_message_includes_constitution_and_existing_entries() -> None:
    from eugene_plexus_identity._generated.common_models import SelfModelEntry

    constitution_yaml = "name: Eugene\npronouns: they/them\n"
    existing = [
        SelfModelEntry(
            id=uuid4(),
            topic="creative-tasks",
            content="I tend to ramble.",
            createdAt=datetime.now(UTC),
        )
    ]
    turns = [
        MemoryMessage(role="user", content="hi", timestamp=datetime.now(UTC)),
        MemoryMessage(role="assistant", content="hello there", timestamp=datetime.now(UTC)),
    ]
    msg = build_reflection_user_message(
        constitution_yaml=constitution_yaml,
        existing_entries=existing,
        recent_turns=turns,
    )
    assert "name: Eugene" in msg
    assert "pronouns: they/them" in msg
    assert "[creative-tasks]" in msg
    assert "I tend to ramble." in msg
    assert "user: hi" in msg
    assert "assistant: hello there" in msg


def test_build_user_message_handles_zero_existing_and_zero_turns() -> None:
    msg = build_reflection_user_message(
        constitution_yaml="name: Eugene\n",
        existing_entries=[],
        recent_turns=[],
    )
    assert "(no prior self-model entries)" in msg
    assert "no recent turns supplied" in msg


# ---------------------------------------------------------------------------
# Fake clients for end-to-end reflection-route tests
# ---------------------------------------------------------------------------


class FakeHemisphereClient:
    """In-process hemisphere client that returns canned response text."""

    def __init__(self, *, canned: str) -> None:
        self.canned = canned
        self.calls: list[tuple[str, str]] = []

    @property
    def base_url(self) -> str:
        return "in-process"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_message: str,
        temperature: float | None = 0.5,
        max_tokens: int | None = 2048,
    ) -> str:
        self.calls.append((system_prompt, user_message))
        return self.canned

    async def aclose(self) -> None:
        return None


class FakeMemoryClient:
    def __init__(self, *, turns: list[MemoryMessage] | None = None) -> None:
        self._turns = list(turns or [])
        self.recent_calls: list[tuple[UUID, int]] = []

    @property
    def base_url(self) -> str:
        return "in-process"

    async def conversation(self, conversation_id: UUID) -> list[MemoryMessage]:
        return list(self._turns)

    async def recent_for_person(self, *, person_id: UUID, limit: int = 50) -> list[MemoryMessage]:
        self.recent_calls.append((person_id, limit))
        return list(self._turns)

    async def aclose(self) -> None:
        return None


class FailingHemisphereClient:
    @property
    def base_url(self) -> str:
        return "in-process"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_message: str,
        temperature: float | None = 0.5,
        max_tokens: int | None = 2048,
    ) -> str:
        raise HemisphereError(status_code=502, detail="hemisphere is down")

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Route-level reflection tests
# ---------------------------------------------------------------------------


@pytest.fixture
def reflection_app(settings: Any) -> FastAPI:
    """Build an app with the FAKE hemisphere + memory clients pre-injected.

    Tests can override `.canned` on the hemisphere or `.turns` on the
    memory client before calling the endpoint.
    """
    from eugene_plexus_identity.app import create_app

    app = create_app(settings=settings)
    app.state.hemisphere_client = FakeHemisphereClient(canned='{"entries": []}')
    app.state.memory_client = FakeMemoryClient()
    return app


def test_reflect_persists_parsed_entries(reflection_app: FastAPI) -> None:
    """End-to-end: hemisphere returns a JSON payload, the route parses
    it, the entries land in SQLite, and the response carries them."""
    hemisphere: FakeHemisphereClient = reflection_app.state.hemisphere_client
    hemisphere.canned = (
        '{"entries": ['
        '{"topic": "creative-tasks", "content": "I tend to ramble."},'
        '{"topic": "uncertainty", "content": "I hedge more than I should."}'
        "]}"
    )

    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    written = body["entriesWritten"]
    assert len(written) == 2
    topics = {e["topic"] for e in written}
    assert topics == {"creative-tasks", "uncertainty"}

    # And the entries are queryable through GET — the route actually
    # persisted, didn't just echo.
    with TestClient(reflection_app) as client:
        list_response = client.get("/v1/identity/self-model?limit=20")
    assert list_response.status_code == 200
    list_topics = {e["topic"] for e in list_response.json()["entries"]}
    assert {"creative-tasks", "uncertainty"}.issubset(list_topics)


def test_reflect_with_empty_entries_returns_empty_list(reflection_app: FastAPI) -> None:
    """Hemisphere may legitimately decide there's nothing new to add."""
    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 200
    assert response.json()["entriesWritten"] == []


def test_reflect_handles_malformed_response_gracefully(
    reflection_app: FastAPI,
) -> None:
    """A junk response from the hemisphere produces zero entries — NOT
    a 5xx. The operator can re-trigger reflection without restarting."""
    reflection_app.state.hemisphere_client = FakeHemisphereClient(
        canned="Sorry, I refuse to do that.",
    )
    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 200
    assert response.json()["entriesWritten"] == []


def test_reflect_503_when_hemisphere_unreachable(reflection_app: FastAPI) -> None:
    reflection_app.state.hemisphere_client = FailingHemisphereClient()
    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["title"] == "Reflection hemisphere unreachable"
    assert "hemisphere is down" in detail["detail"]


def test_reflect_resolves_operator_when_no_conversation_id(
    reflection_app: FastAPI,
) -> None:
    """No conversationId in the body → reflection scopes to the
    operator's recent turns. Identity calls `ensure_operator()` to
    get the personId, then memory.recent_for_person()."""
    memory: FakeMemoryClient = reflection_app.state.memory_client
    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 200
    assert len(memory.recent_calls) == 1
    person_id, limit = memory.recent_calls[0]
    assert isinstance(person_id, UUID)
    assert limit == 50  # default reflectionMaxLookbackTurns


def test_reflect_honors_explicit_lookback(reflection_app: FastAPI) -> None:
    memory: FakeMemoryClient = reflection_app.state.memory_client
    with TestClient(reflection_app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={"lookbackTurns": 5})
    assert response.status_code == 200
    assert memory.recent_calls[0][1] == 5


def test_reflect_503_in_safe_mode(settings: Any, tmp_path: Path) -> None:
    """Safe mode disables reflection regardless of config — the
    endpoint returns 503 with a recovery-flow message."""
    from eugene_plexus_identity.app import create_app
    from eugene_plexus_identity.settings import Settings

    settings_safe = Settings(
        config_file=tmp_path / "config.yaml",
        constitution_file=tmp_path / "constitution.yaml",
        db_file=tmp_path / "identity.db",
        safe_mode=True,
    )
    app = create_app(settings=settings_safe)
    # Even with a hemisphere client wired in, safe-mode wins.
    app.state.hemisphere_client = FakeHemisphereClient(canned='{"entries": []}')

    with TestClient(app) as client:
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 503
    assert "safe mode" in response.json()["detail"]["title"].lower()


def test_reflect_prompt_includes_constitution_and_memory_turns(
    reflection_app: FastAPI,
) -> None:
    """The hemisphere sees the full assembled prompt — verify the
    expected sections appear."""
    # Pre-seed the memory client's turns before lifespan runs.
    memory: FakeMemoryClient = reflection_app.state.memory_client
    memory._turns = [
        MemoryMessage(role="user", content="REMEMBER_THIS_USER", timestamp=datetime.now(UTC)),
        MemoryMessage(
            role="assistant",
            content="REMEMBER_THIS_ASSISTANT",
            timestamp=datetime.now(UTC),
        ),
    ]

    with TestClient(reflection_app) as client:
        # Update constitution inside the lifespan-active context.
        reflection_app.state.constitution_store.update(
            {
                "name": "Eugene",
                "pronouns": "they/them",
                "coreValues": ["honesty"],
            }
        )
        response = client.post("/v1/identity/self-model/reflect", json={})
    assert response.status_code == 200

    hemisphere: FakeHemisphereClient = reflection_app.state.hemisphere_client
    assert len(hemisphere.calls) == 1
    _system, user_msg = hemisphere.calls[0]
    assert "name: Eugene" in user_msg
    assert "REMEMBER_THIS_USER" in user_msg
    assert "REMEMBER_THIS_ASSISTANT" in user_msg
