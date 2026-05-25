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

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
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


# How long we'll wait for any single lifespan step (sqlite open, peer
# auto-resolve, etc.) before treating it as a stall. A hard timeout
# turns a silent hang into a visible failure: the operator sees a
# traceback in the watchdog log, the crash counter increments, and
# eventually backoff kicks in — vs. the v0.2 bug where identity hung
# at "Waiting for application startup" forever with no signal at all.
_LIFESPAN_STEP_TIMEOUT_SECONDS = 30.0


async def resolve_peer_url(
    *,
    kind: str,
    watchdog_url: str,
    service_token: str | None,
    timeout_seconds: float = 5.0,
) -> str | None:
    """Ask the watchdog where a peer component lives.

    The watchdog is the source of truth for body-component topology;
    duplicating URLs in every component's config is the OpenClaw-style
    trap we're avoiding. For identity, this resolves reflection peers
    (a hemisphere-driver and memory) when the operator hasn't pinned
    explicit URLs via config.

    Returns the peer's URL (trailing slash stripped) or None when the
    watchdog can't be reached / has no entry of that kind. For kinds
    with multiple instances (hemisphere-driver), the FIRST entry wins;
    operator overrides via config to pin a specific one.
    """
    headers = {"Authorization": f"Bearer {service_token}"} if service_token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                f"{watchdog_url.rstrip('/')}/v1/components",
                headers=headers,
            )
        if response.status_code >= 400:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    components = body.get("components") if isinstance(body, dict) else None
    if not isinstance(components, list):
        return None
    for c in components:
        if isinstance(c, dict) and c.get("kind") == kind:
            url = c.get("url")
            if isinstance(url, str) and url:
                return url.rstrip("/")
    return None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # Lifespan-checkpoint logging: every step that could plausibly
    # hang (file I/O, sqlite open, async peer-URL resolution) emits a
    # log line on entry. The v0.2 startup-hang bug
    # ([[project_identity_startup_hang_v02]]) was a silent stall
    # between "Waiting for application startup" and "Application
    # startup complete" with NO traceback — adding these checkpoints
    # so the next occurrence pinpoints WHICH step stalled.
    log.info("lifespan: building auth_state")
    if not hasattr(app.state, "auth_state"):
        app.state.auth_state = load_auth_state(
            signing_key_b64=settings.auth_signing_key,
            service_token=settings.service_token,
            master_key_b64=settings.master_key,
        )

    log.info("lifespan: loading config from %s", settings.config_file)
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
    log.info("lifespan: loading constitution from %s", settings.constitution_file)
    constitution_store = ConstitutionStore(settings.constitution_file)
    if not settings.safe_mode:
        constitution_store.load()
    app.state.constitution_store = constitution_store

    # SQLite identity store. Safe mode skips opening so a corrupted DB
    # can't block /v1/config — the routes that need it (persons /
    # self-model / links) will produce a 500 if hit, but the operator
    # can still fix config and restart.
    log.info("lifespan: opening sqlite identity store at %s", settings.db_file)
    identity_store = IdentityStore(settings.db_file)
    if not settings.safe_mode:
        # `IdentityStore.load()` is synchronous (sqlite3.connect +
        # schema). On a stock install it returns in <10ms; on a wedged
        # DB (lock left by a killed prior process, fs issue) it can
        # block forever with no Python error surface. Run it in a
        # worker thread under wait_for so the lifespan can't be stalled
        # silently — a timeout becomes a TimeoutError traceback the
        # operator can act on.
        await asyncio.wait_for(
            asyncio.to_thread(identity_store.load),
            timeout=_LIFESPAN_STEP_TIMEOUT_SECONDS,
        )
        log.info("lifespan: ensuring operator person exists")
        # Ensure the operator person exists at startup. Without this,
        # the orchestrator's `_resolve_operator_person_id` returns None
        # on every chat turn, effective_person_id falls back to
        # NIL_PERSON_ID, and `person_recent` is skipped — Eugene has
        # no cross-conversation recall of who you are.
        # Idempotent: a no-op when an operator already exists, so a
        # fresh install gets one and a re-install preserves the prior
        # operator UUID + aliases. Default displayName is "Operator";
        # the operator can rename via PATCH /v1/identity/persons/{id}.
        await asyncio.wait_for(
            asyncio.to_thread(identity_store.ensure_operator),
            timeout=_LIFESPAN_STEP_TIMEOUT_SECONDS,
        )
    app.state.identity_store = identity_store

    # Reflection clients. Both are optional — if neither URL is
    # configured AND the watchdog has nothing to auto-resolve, the
    # reflect endpoint returns 503 with an actionable message. Tests
    # can pre-populate `app.state.hemisphere_client` /
    # `app.state.memory_client` with fakes.
    #
    # Auto-resolve order: explicit config value → watchdog
    # /v1/components → None. Same trap-avoidance pattern the
    # orchestrator and connector use for their peers.
    auth: AuthState = app.state.auth_state

    async def _resolve(kind: str, config_key: str) -> str | None:
        explicit = str(config_store.get(config_key) or "").strip()
        if explicit:
            return explicit
        if settings.safe_mode:
            return None
        # `resolve_peer_url` already has its own 5s httpx timeout; the
        # outer wait_for is belt-and-suspenders against an httpx event-
        # loop hang (suspected v0.2 culprit) outliving the inner timeout.
        try:
            resolved = await asyncio.wait_for(
                resolve_peer_url(
                    kind=kind,
                    watchdog_url=settings.watchdog_url,
                    service_token=auth.service_token,
                ),
                timeout=_LIFESPAN_STEP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning(
                "auto-resolve of %s from watchdog timed out after %.0fs — "
                "continuing without (set %s explicitly in identity config "
                "to bypass)",
                kind,
                _LIFESPAN_STEP_TIMEOUT_SECONDS,
                config_key,
            )
            return None
        if resolved:
            log.info(
                "auto-resolved %s from watchdog: %s (config field %s was unset)",
                kind,
                resolved,
                config_key,
            )
        return resolved

    owns_hemisphere = False
    if not hasattr(app.state, "hemisphere_client"):
        log.info("lifespan: resolving reflection hemisphere peer URL")
        hemi_url = await _resolve("hemisphere-driver", "reflectionHemisphereUrl")
        if hemi_url:
            app.state.hemisphere_client = HemisphereClient(
                base_url=hemi_url,
                service_token=auth.service_token,
            )
            owns_hemisphere = True
        else:
            app.state.hemisphere_client = None
    owns_memory = False
    if not hasattr(app.state, "memory_client"):
        log.info("lifespan: resolving reflection memory peer URL")
        mem_url = await _resolve("memory", "reflectionMemoryUrl")
        if mem_url:
            app.state.memory_client = MemoryClient(
                base_url=mem_url,
                service_token=auth.service_token,
            )
            owns_memory = True
        else:
            app.state.memory_client = None
    log.info("lifespan: ready (yielding)")

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
