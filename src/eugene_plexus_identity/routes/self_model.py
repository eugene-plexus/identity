"""Self-model: GET /v1/identity/self-model, POST .../reflect."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from .._generated.common_models import Problem, SelfModelEntry
from ..store import IdentityStore

router = APIRouter(tags=["self-model"])


def _problem(status_code: int, title: str, detail: str) -> HTTPException:
    slug = title.replace(" ", "-").lower()
    return HTTPException(
        status_code=status_code,
        detail=Problem(
            type=f"https://github.com/eugene-plexus/identity#{slug}",
            title=title,
            status=status_code,
            detail=detail,
            component="identity",
        ).model_dump(exclude_none=True),
    )


class _SelfModelResponse(BaseModel):
    entries: list[SelfModelEntry]


@router.get("/v1/identity/self-model", response_model=_SelfModelResponse)
async def query_self_model(
    request: Request,
    topic: str | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=50),
    personId: UUID | None = Query(default=None),
) -> _SelfModelResponse:
    store: IdentityStore = request.app.state.identity_store
    entries = store.list_self_model(topic=topic, person_id=personId, limit=limit)
    return _SelfModelResponse(entries=entries)


@router.post("/v1/identity/self-model/reflect", status_code=501)
async def reflect_and_write_self_model(request: Request) -> None:
    """v0.2 skeleton: reflection requires a configured hemisphere-driver
    client to actually run; that integration lands in a follow-up.
    Returns 501 with a clear message so the UI can surface "not yet
    available in this build" rather than a generic error.
    """
    raise _problem(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Not Implemented",
        "Reflection requires a configured hemisphere-driver to call "
        "into. That wiring is a v0.2 follow-up. Self-model entries can "
        "still be written directly via the storage layer for now.",
    )
