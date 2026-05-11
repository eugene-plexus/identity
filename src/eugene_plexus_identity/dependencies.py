"""FastAPI dependencies for v0.2 bearer auth.

Three dependencies cover the identity component's auth matrix:

  * `require_authorized` — operator OR any `service:*` audience.
    For routes any logged-in caller may hit (GET constitution,
    GET persons, GET self-model, GET relationship, etc.).

  * `require_operator` — operator audience only. Constitution PATCH,
    person mutations, link approval / rejection, config edits, admin
    restart. The operator is the only one allowed to shape the
    identity / topology graph.

  * `require_service` — service audience only. Filing a new pending
    link via `POST /v1/identity/links/pending` — only connector
    adapters should introduce new identities; an operator session
    forging this would bypass the approval flow.

All three pass-through when `AuthState.auth_disabled` is true (dev path).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import security
from ._generated.common_models import Problem
from .auth_state import AuthState

_bearer_scheme = HTTPBearer(auto_error=False)


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


def _validate(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
    *,
    accept_operator: bool,
    accept_any_service: bool,
) -> security.TokenPayload | None:
    auth: AuthState = request.app.state.auth_state
    if auth.auth_disabled:
        return None
    if creds is None or not creds.credentials:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Missing token",
            "Provide a bearer token via the Authorization: Bearer header.",
        )
    assert auth.signing_key is not None
    try:
        return security.decode_token(
            token=creds.credentials,
            signing_key=auth.signing_key,
            accept_operator=accept_operator,
            accept_any_service=accept_any_service,
        )
    except Exception as e:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid token",
            f"Bearer token rejected: {e}",
        ) from e


def require_authorized(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> security.TokenPayload | None:
    """Operator OR any service-audience token accepted."""
    return _validate(request, creds, accept_operator=True, accept_any_service=True)


def require_operator(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> security.TokenPayload | None:
    """Operator-audience tokens only — for identity-shaping endpoints."""
    return _validate(request, creds, accept_operator=True, accept_any_service=False)


def require_service(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> security.TokenPayload | None:
    """Any service-audience token accepted; operator rejected.

    Applied to `POST /v1/identity/links/pending` — only connector
    adapters should be filing new pending identities. An operator
    session forging this would bypass the approval flow's spoof
    protections.
    """
    return _validate(request, creds, accept_operator=False, accept_any_service=True)
