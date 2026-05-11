"""POST /v1/admin/restart — schedule a process exit for the supervisor.

Mirrors the same endpoint on orchestrator, hemisphere-driver, memory.
The UI's Restart Now flow hits this when an operator changes a
restart-required config field (`logLevel`, future paths, etc).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter

from .._generated.common_models import RestartResult

router = APIRouter(tags=["admin"])

_EXIT_DELAY_MS = 500


@router.post("/v1/admin/restart", response_model=RestartResult, status_code=202)
async def restart() -> RestartResult:
    log = logging.getLogger(__name__)
    log.warning("restart requested via /v1/admin/restart; exiting in %dms", _EXIT_DELAY_MS)

    loop = asyncio.get_event_loop()
    loop.call_later(_EXIT_DELAY_MS / 1000.0, lambda: os._exit(0))

    return RestartResult(
        scheduled=True,
        delayMs=_EXIT_DELAY_MS,
        message=(
            f"Process exiting in {_EXIT_DELAY_MS}ms. A supervisor (the "
            "watchdog in personal installs; systemd / docker in larger "
            "deploys) is expected to relaunch it."
        ),
    )
