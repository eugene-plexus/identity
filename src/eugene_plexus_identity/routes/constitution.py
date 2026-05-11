"""GET / PATCH /v1/identity/constitution.

Constitution is operator-editable from the UI. Eugene himself cannot
modify it — `PATCH` is gated by `require_operator` at the app layer,
service tokens reach the route only via `GET` (which any logged-in
caller can read so the orchestrator can fold it into hemisphere
prompts).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .._generated.common_models import Constitution, Problem
from ..dependencies import require_operator
from ..store import ConstitutionStore

router = APIRouter(tags=["constitution"])


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


@router.get("/v1/identity/constitution", response_model=Constitution)
async def get_constitution(request: Request) -> Constitution:
    store: ConstitutionStore = request.app.state.constitution_store
    return store.get()


@router.patch(
    "/v1/identity/constitution",
    response_model=Constitution,
    dependencies=[Depends(require_operator)],
)
async def update_constitution(
    request: Request, body: Constitution
) -> Constitution:
    """Partial update. Body is a `Constitution` shape with any subset
    of fields supplied; unsupplied fields keep their current value.
    """
    store: ConstitutionStore = request.app.state.constitution_store
    patch = body.model_dump(exclude_unset=True)
    try:
        return store.update(patch)
    except ValueError as e:
        raise _problem(
            status.HTTP_400_BAD_REQUEST,
            "Invalid constitution",
            str(e),
        ) from e
