"""Pending identity links: list / file / approve / reject.

The audience matrix here is the most carefully scoped on this
component:

  * `GET /v1/identity/links/pending` — operator OR service. UI polls
    to show the badge; service tokens may read (e.g. an adapter
    checking whether its prior pending submission is still pending).
  * `POST /v1/identity/links/pending` — SERVICE-ONLY. Only adapters
    introduce unknown identities; an operator session forging this
    would bypass the spoof-resistance design.
  * `POST .../approve` and `POST .../reject` — OPERATOR-ONLY.
    The whole point of the approval flow is operator confirmation;
    a leaked service token can't approve itself onto an existing
    person.

The audience splitting is applied at the route level (`Depends`)
rather than in the app's blanket router-level deps so the matrix is
inspectable here.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .._generated.common_models import (
    LinkApprovalRequest,
    PendingIdentityLink,
    Person,
    Problem,
    Status1,
)
from ..dependencies import require_operator, require_service
from ..store import IdentityStore

router = APIRouter(tags=["links"])


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


class _PendingLinksResponse(BaseModel):
    links: list[PendingIdentityLink]


class _ApprovalResponse(BaseModel):
    link: PendingIdentityLink
    person: Person


@router.get(
    "/v1/identity/links/pending",
    response_model=_PendingLinksResponse,
)
async def list_pending_links(request: Request) -> _PendingLinksResponse:
    store: IdentityStore = request.app.state.identity_store
    return _PendingLinksResponse(links=store.list_pending_links())


@router.post(
    "/v1/identity/links/pending",
    response_model=PendingIdentityLink,
    status_code=201,
    dependencies=[Depends(require_service)],
)
async def create_pending_link(
    request: Request, body: PendingIdentityLink
) -> PendingIdentityLink:
    store: IdentityStore = request.app.state.identity_store
    existing = store.find_pending_for(body.platform, body.accountId)
    if existing is not None:
        # Per the spec contract: a duplicate is a 409 the adapter
        # treats as "the operator just hasn't approved yet". We return
        # the existing link so the adapter can correlate.
        raise _problem(
            status.HTTP_409_CONFLICT,
            "Already pending",
            f"A pending link already exists for ({body.platform}, "
            f"{body.accountId}).",
        )
    return store.create_pending_link(body)


@router.post(
    "/v1/identity/links/pending/{link_id}/approve",
    response_model=_ApprovalResponse,
    dependencies=[Depends(require_operator)],
)
async def approve_pending_link(
    request: Request, link_id: UUID, body: LinkApprovalRequest
) -> _ApprovalResponse:
    store: IdentityStore = request.app.state.identity_store
    link = store.get_pending_link(link_id)
    if link is None:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Link not found",
            f"No pending link with id {link_id}.",
        )

    # The approval body has two modes — alias onto an existing person
    # OR create a new person record. Exactly one of (linkAsPersonId,
    # displayName) must be supplied; both / neither is a 400.
    use_existing = body.linkAsPersonId is not None
    if use_existing == bool(body.displayName):
        raise _problem(
            status.HTTP_400_BAD_REQUEST,
            "Invalid approval request",
            "Supply exactly one of `linkAsPersonId` (alias onto an "
            "existing person) or `displayName` (create a new person).",
        )

    if use_existing:
        assert body.linkAsPersonId is not None
        target = store.get_person(body.linkAsPersonId)
        if target is None:
            raise _problem(
                status.HTTP_400_BAD_REQUEST,
                "Unknown person",
                f"No person with id {body.linkAsPersonId}.",
            )
        person = target
    else:
        assert body.displayName is not None
        person = store.create_person(
            display_name=body.displayName,
            relationship_note=body.relationshipNote,
        )

    store.add_alias(
        person_id=person.personId,
        platform=link.platform,
        account_id=link.accountId,
        handle=link.handle,
        display_name=link.displayName,
        avatar_url=str(link.avatarUrl) if link.avatarUrl else None,
    )
    updated_link = store.update_link_status(link_id, Status1.approved)
    assert updated_link is not None

    # Re-fetch the person to pick up the newly-attached alias.
    refreshed = store.get_person(person.personId)
    assert refreshed is not None
    return _ApprovalResponse(link=updated_link, person=refreshed)


@router.post(
    "/v1/identity/links/pending/{link_id}/reject",
    status_code=204,
    dependencies=[Depends(require_operator)],
)
async def reject_pending_link(request: Request, link_id: UUID) -> None:
    store: IdentityStore = request.app.state.identity_store
    updated = store.update_link_status(link_id, Status1.rejected)
    if updated is None:
        raise _problem(
            status.HTTP_404_NOT_FOUND,
            "Link not found",
            f"No pending link with id {link_id}.",
        )
