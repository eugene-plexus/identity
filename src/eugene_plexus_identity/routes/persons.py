"""Persons CRUD + per-person relationship summary."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .._generated.common_models import (
    Person,
    Problem,
    RelationshipSummary,
)
from ..dependencies import require_operator
from ..store import IdentityStore

router = APIRouter(tags=["persons"])


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


class _PersonsResponse(BaseModel):
    persons: list[Person]


class _CreatePersonRequest(BaseModel):
    displayName: str = Field(min_length=1)
    relationshipNote: str | None = None


class _UpdatePersonRequest(BaseModel):
    displayName: str | None = Field(default=None, min_length=1)
    relationshipNote: str | None = None


@router.get("/v1/identity/persons", response_model=_PersonsResponse)
async def list_persons(request: Request) -> _PersonsResponse:
    store: IdentityStore = request.app.state.identity_store
    return _PersonsResponse(persons=store.list_persons())


@router.post(
    "/v1/identity/persons",
    response_model=Person,
    status_code=201,
    dependencies=[Depends(require_operator)],
)
async def create_person(
    request: Request, body: _CreatePersonRequest
) -> Person:
    store: IdentityStore = request.app.state.identity_store
    return store.create_person(
        display_name=body.displayName,
        relationship_note=body.relationshipNote,
    )


@router.get("/v1/identity/persons/{person_id}", response_model=Person)
async def get_person(request: Request, person_id: UUID) -> Person:
    store: IdentityStore = request.app.state.identity_store
    person = store.get_person(person_id)
    if person is None:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Person not found",
            f"No person with id {person_id}.",
        )
    return person


@router.patch(
    "/v1/identity/persons/{person_id}",
    response_model=Person,
    dependencies=[Depends(require_operator)],
)
async def update_person(
    request: Request, person_id: UUID, body: _UpdatePersonRequest
) -> Person:
    store: IdentityStore = request.app.state.identity_store
    updated = store.update_person(
        person_id,
        display_name=body.displayName,
        relationship_note=body.relationshipNote,
    )
    if updated is None:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Person not found",
            f"No person with id {person_id}.",
        )
    return updated


@router.delete(
    "/v1/identity/persons/{person_id}",
    status_code=204,
    dependencies=[Depends(require_operator)],
)
async def delete_person(request: Request, person_id: UUID) -> None:
    store: IdentityStore = request.app.state.identity_store
    try:
        removed = store.delete_person(person_id)
    except PermissionError as e:
        raise _problem(
            status.HTTP_409_CONFLICT,
            "Cannot delete operator",
            str(e),
        ) from e
    if not removed:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Person not found",
            f"No person with id {person_id}.",
        )


@router.get(
    "/v1/identity/persons/{person_id}/relationship",
    response_model=RelationshipSummary,
)
async def get_person_relationship(
    request: Request, person_id: UUID
) -> RelationshipSummary:
    """v0.2 returns a minimal summary: just the personId + lastUpdated
    + turnCount=0. The orchestrator integrates recent turns from the
    memory component itself when assembling hemisphere prompts; the
    `recentTurns` field on this endpoint stays empty until v0.3 wires
    identity → memory directly.
    """
    store: IdentityStore = request.app.state.identity_store
    person = store.get_person(person_id)
    if person is None:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Person not found",
            f"No person with id {person_id}.",
        )
    return RelationshipSummary(
        personId=person.personId,
        turnCount=0,
        lastUpdated=datetime.now(UTC),
        recentTurns=[],
    )
