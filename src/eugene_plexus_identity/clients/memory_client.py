"""Memory read client for the reflection flow.

Two read paths:

  - `conversation(id)` — fetch a specific conversation's messages.
    Used when the operator triggers reflection on one focused chat.
  - `recent_for_person(person_id, limit)` — newest-first MemoryEntry
    rows for a person across all conversations. Used for the general
    "reflect on all recent activity" path; identity calls this with
    the operator's personId from its own persons table.

Both raise `httpx.HTTPError` on transport failure; the reflection
service maps that to 503 with an actionable detail.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import httpx
from pydantic import BaseModel, Field


class MemoryMessage(BaseModel):
    """Lightweight projection of memory's response shape.

    We define this locally rather than importing memory's generated
    types — identity's codegen only pulls in identity.yaml +
    common.yaml; the wire-level memory types are sourced ad-hoc.
    """

    role: str
    content: str
    timestamp: datetime | None = None
    personId: UUID | None = Field(default=None, alias="personId")


class MemoryClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 30.0,
        service_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else None
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=headers,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def conversation(self, conversation_id: UUID) -> list[MemoryMessage]:
        response = await self._client.get(f"/v1/conversations/{conversation_id}")
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        out: list[MemoryMessage] = []
        for raw in body.get("messages", []):
            try:
                out.append(MemoryMessage.model_validate(raw))
            except Exception:
                # Skip malformed entries rather than blowing up the
                # whole reflection — defensive against memory backend
                # version skew.
                continue
        return out

    async def recent_for_person(self, *, person_id: UUID, limit: int = 50) -> list[MemoryMessage]:
        response = await self._client.get(
            f"/v1/memory/persons/{person_id}/recent",
            params={"limit": limit},
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        # `/recent` returns MemoryEntry rows (newest-first). They're a
        # superset of MemoryMessage — keep just the fields we need.
        out: list[MemoryMessage] = []
        for entry in body.get("entries", []):
            try:
                out.append(
                    MemoryMessage(
                        role=entry["role"],
                        content=entry["content"],
                        timestamp=(
                            datetime.fromisoformat(entry["timestamp"])
                            if entry.get("timestamp")
                            else None
                        ),
                        personId=(UUID(entry["personId"]) if entry.get("personId") else None),
                    )
                )
            except Exception:
                continue
        # Memory returns newest-first; reflection wants chronological.
        return list(reversed(out))

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["MemoryClient", "MemoryMessage"]
