"""Startup-time settings, sourced from environment variables.

Distinct from runtime *config* (see `config.py`), which is editable via
`PATCH /v1/config`. These settings only control bootstrap.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EUGENE_PLEXUS_IDENTITY_",
        env_file=None,
        case_sensitive=False,
    )

    config_file: Path = Path("config.yaml")
    """Where the runtime config is persisted. PATCH /v1/config writes here."""

    constitution_file: Path = Path("constitution.yaml")
    """Where the operator-editable Constitution lives. YAML so it's
    inspectable / version-controllable. Operator may edit by hand
    when the UI isn't reachable."""

    db_file: Path = Path("identity.db")
    """SQLite file holding persons, platform aliases, self-model
    entries, and pending links."""

    bind_host: str = "127.0.0.1"
    """Network interface to bind. Override to 0.0.0.0 for tailnet exposure."""

    safe_mode: bool = False
    """If true, skip loading the persisted config file at startup and run on
    built-in defaults. Set by the watchdog via
    EUGENE_PLEXUS_IDENTITY_SAFE_MODE=1 when a previous boot failed. PATCH
    /v1/config still writes to `config_file` normally so the operator's
    repair survives the next non-safe-mode boot. Per the safe-mode
    contract in specs/openapi/identity.yaml."""

    auth_signing_key: str | None = None
    """Base64-encoded 32-byte HMAC signing key, supplied by the watchdog
    at spawn time (EUGENE_PLEXUS_IDENTITY_AUTH_SIGNING_KEY). When absent
    the component runs unauthenticated — dev / standalone path only."""

    service_token: str | None = None
    """Long-lived service JWT (EUGENE_PLEXUS_IDENTITY_SERVICE_TOKEN).
    Captured-but-unused in v0.2 (identity makes no outbound calls); the
    watchdog supplies it for symmetry with other kinds and so v0.3's
    reflection flow can call hemisphere-driver with it."""

    master_key: str | None = None
    """Base64-encoded 32-byte secretbox key
    (EUGENE_PLEXUS_IDENTITY_MASTER_KEY). Not used in v0.2 — identity
    doesn't currently store anything that needs at-rest encryption."""


def load_settings() -> Settings:
    return Settings()
