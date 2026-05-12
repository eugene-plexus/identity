"""Self-model: GET /v1/identity/self-model, POST .../reflect."""

from __future__ import annotations

from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from .._generated.common_models import Problem, SelfModelEntry
from ..clients.hemisphere_client import HemisphereError
from ..reflection import ReflectionConfigError, run_reflection
from ..store import ConstitutionStore, IdentityStore

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


class _ReflectRequest(BaseModel):
    lookbackTurns: int | None = None
    conversationId: UUID | None = None


class _ReflectResponse(BaseModel):
    entriesWritten: list[SelfModelEntry]


@router.post("/v1/identity/self-model/reflect", response_model=_ReflectResponse)
async def reflect_and_write_self_model(
    request: Request, body: _ReflectRequest | None = None
) -> _ReflectResponse:
    """Trigger Eugene's reflection process.

    Reads `lookbackTurns` recent memory turns (with the operator if
    no conversationId is supplied), asks the configured hemisphere-
    driver to extract autobiographical observations, and persists
    them as `SelfModelEntry` rows. Returns the new entries (NOT all
    entries — caller can re-query for the full set).

    Returns 503 when the operator hasn't configured
    `reflectionHemisphereUrl`, when the hemisphere-driver is
    unreachable, or when identity is in safe mode.
    """
    if getattr(request.app.state, "safe_mode", False):
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Identity in safe mode",
            "Reflection is disabled while identity is in safe mode "
            "(EUGENE_PLEXUS_IDENTITY_SAFE_MODE=1). Fix config via "
            "/v1/config and restart without the env var.",
        )

    constitution_store: ConstitutionStore = request.app.state.constitution_store
    identity_store: IdentityStore = request.app.state.identity_store
    hemisphere_client = getattr(request.app.state, "hemisphere_client", None)
    memory_client = getattr(request.app.state, "memory_client", None)
    config_store = request.app.state.config_store

    body = body or _ReflectRequest()
    default_lookback = int(config_store.get("reflectionMaxLookbackTurns") or 50)
    lookback = body.lookbackTurns or default_lookback

    # If no conversationId is supplied, the reflection scopes to the
    # operator's recent activity. Resolve operator personId from the
    # store; ensure_operator() creates one on first call so a fresh
    # install still works.
    related_person_id: UUID | None = None
    if body.conversationId is None:
        operator = identity_store.ensure_operator()
        related_person_id = operator.personId

    try:
        result = await run_reflection(
            constitution_store=constitution_store,
            identity_store=identity_store,
            hemisphere_client=hemisphere_client,
            memory_client=memory_client,
            lookback_turns=lookback,
            conversation_id=body.conversationId,
            related_person_id=related_person_id,
        )
    except ReflectionConfigError as e:
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Reflection not configured",
            e.detail,
        ) from e
    except HemisphereError as e:
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Reflection hemisphere unreachable",
            e.detail,
        ) from e
    except httpx.HTTPError as e:
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Reflection upstream unreachable",
            f"Network failure during reflection: {e!r}",
        ) from e

    return _ReflectResponse(entriesWritten=result.entries_written)
