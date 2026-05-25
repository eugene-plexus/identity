"""CLI entry point: `python -m eugene_plexus_identity`."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    # uvicorn's `log_level` only sets up the `uvicorn.*` loggers; the
    # root logger stays at WARNING, which means our own
    # `eugene_plexus_identity.*` INFO records get dropped silently.
    # Setting the root level here (before uvicorn.run) ensures the
    # lifespan checkpoint lines and route-level info logs actually
    # reach stdout — critical when the watchdog is piping our output
    # for `[identity]`-prefixed display.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    app = create_app(settings=settings)
    # Bind port: prefer the env var threaded in by the watchdog supervisor;
    # fall back to the canonical identity port for standalone dev runs.
    port = int(os.environ.get("EUGENE_PLEXUS_IDENTITY_BIND_PORT", "8084"))
    uvicorn.run(app, host=settings.bind_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
