"""FastAPI app factory.

Lifespan:
  1. Build AuthState from env (signing key / service token / master
     key supplied by the watchdog at spawn time).
  2. Construct the ConfigStore and load it from disk (in safe mode
     the on-disk file is skipped — operator's defaults stay
     editable via /v1/config so they can repair via UI).
  3. Construct the ConstitutionStore (operator-editable YAML) and
     load it; an empty install gets a minimal `name: Eugene`
     constitution written to disk.
  4. Open the SQLite-backed IdentityStore (persons / aliases /
     self-model / pending links). Safe mode skips the open so a
     corrupted DB can't lock the operator out of /v1/config.

Routing:
  - `/healthz` is the only unauthenticated route.
  - GET endpoints (constitution, self-model, persons, pending links)
    accept operator OR any service-audience token — both UI and peer
    components may read.
  - Constitution PATCH, persons mutations, config edits, admin
    restart all require the operator audience.
  - Filing a new pending link (`POST /v1/identity/links/pending`)
    is service-audience-only — only adapters introduce identities.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from . import __version__
from .auth_state import AuthState, load_auth_state
from .clients import HemisphereClient, MemoryClient
from .config import ConfigStore
from .dependencies import require_authorized, require_operator
from .routes import admin as admin_routes
from .routes import config as config_routes
from .routes import constitution as constitution_routes
from .routes import health as health_routes
from .routes import links as links_routes
from .routes import persons as persons_routes
from .routes import self_model as self_model_routes
from .settings import Settings, load_settings
from .store import ConstitutionStore, IdentityStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    if not hasattr(app.state, "auth_state"):
        app.state.auth_state = load_auth_state(
            signing_key_b64=settings.auth_signing_key,
            service_token=settings.service_token,
            master_key_b64=settings.master_key,
        )

    config_store = ConfigStore(settings.config_file)
    if settings.safe_mode:
        log.warning(
            "starting in SAFE MODE (EUGENE_PLEXUS_IDENTITY_SAFE_MODE=1); "
            "ignoring %s and running on defaults. Fix config via "
            "/v1/config, then restart without the env var.",
            settings.config_file,
        )
    else:
        config_store.load()
    app.state.config_store = config_store
    app.state.safe_mode = settings.safe_mode

    # Constitution: always reachable for reads (every chat turn needs
    # at least `name`). Safe mode skips the disk read and serves the
    # default constitution; the operator's persisted file is preserved.
    constitution_store = ConstitutionStore(settings.constitution_file)
    if not settings.safe_mode:
        constitution_store.load()
    app.state.constitution_store = constitution_store

    # SQLite identity store. Safe mode skips opening so a corrupted DB
    # can't block /v1/config — the routes that need it (persons /
    # self-model / links) will produce a 500 if hit, but the operator
    # can still fix config and restart.
    identity_store = IdentityStore(settings.db_file)
    if not settings.safe_mode:
        identity_store.load()
    app.state.identity_store = identity_store

    # Reflection clients. Both are optional — if neither URL is
    # configured, the reflect endpoint returns 503 with an actionable
    # message. Tests can pre-populate `app.state.hemisphere_client` /
    # `app.state.memory_client` with fakes.
    auth: AuthState = app.state.auth_state
    owns_hemisphere = False
    if not hasattr(app.state, "hemisphere_client"):
        hemi_url = config_store.get("reflectionHemisphereUrl") if not settings.safe_mode else None
        if hemi_url:
            app.state.hemisphere_client = HemisphereClient(
                base_url=str(hemi_url),
                service_token=auth.service_token,
            )
            owns_hemisphere = True
        else:
            app.state.hemisphere_client = None
    owns_memory = False
    if not hasattr(app.state, "memory_client"):
        mem_url = config_store.get("reflectionMemoryUrl") if not settings.safe_mode else None
        if mem_url:
            app.state.memory_client = MemoryClient(
                base_url=str(mem_url),
                service_token=auth.service_token,
            )
            owns_memory = True
        else:
            app.state.memory_client = None

    try:
        yield
    finally:
        identity_store.close()
        if owns_hemisphere and app.state.hemisphere_client is not None:
            await app.state.hemisphere_client.aclose()
        if owns_memory and app.state.memory_client is not None:
            await app.state.memory_client.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app with all routers mounted."""
    settings = settings or load_settings()

    app = FastAPI(
        title="Eugene Plexus — identity",
        description="Constitution, self-model, persons, and the pending-links queue.",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    # /healthz is always open — supervisors and load balancers probe
    # this without credentials.
    app.include_router(health_routes.router)

    # Operator-only surfaces: config edits, admin restart, and
    # constitution PATCH. Constitution GET is mixed-audience so we
    # mount the router twice — once for the operator-only PATCH
    # action via a route-level dep, falling through to the
    # require_authorized layer below for GET.
    operator_only = [Depends(require_operator)]
    app.include_router(config_routes.router, dependencies=operator_only)
    app.include_router(admin_routes.router, dependencies=operator_only)

    # Mixed-audience surfaces: GETs for constitution / self-model /
    # persons / pending links, plus the operator-only mutations on
    # persons (per-route `Depends` inside the router enforce the
    # stricter audience where needed). Links router applies its own
    # per-route auth at function level (file vs approve vs reject).
    authorized = [Depends(require_authorized)]
    app.include_router(constitution_routes.router, dependencies=authorized)
    app.include_router(self_model_routes.router, dependencies=authorized)
    app.include_router(persons_routes.router, dependencies=authorized)
    app.include_router(links_routes.router, dependencies=authorized)

    return app
