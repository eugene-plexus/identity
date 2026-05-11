"""CLI entry point: `python -m eugene_plexus_identity`."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    settings = load_settings()
    app = create_app(settings=settings)
    # Bind port: prefer the env var threaded in by the watchdog supervisor;
    # fall back to the canonical identity port for standalone dev runs.
    port = int(os.environ.get("EUGENE_PLEXUS_IDENTITY_BIND_PORT", "8084"))
    uvicorn.run(app, host=settings.bind_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
