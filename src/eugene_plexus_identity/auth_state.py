"""Auth state for the identity component's verify-only role.

Built once at startup from env vars the watchdog threads in:

  * `EUGENE_PLEXUS_IDENTITY_AUTH_SIGNING_KEY` — base64 of the 32-byte
    HMAC key used to validate inbound bearer tokens.
  * `EUGENE_PLEXUS_IDENTITY_SERVICE_TOKEN` — long-lived JWT
    (`aud: service:identity`). v0.2 captures but doesn't use; reserved
    for v0.3 when the reflection flow calls hemisphere-driver.
  * `EUGENE_PLEXUS_IDENTITY_MASTER_KEY` — base64 secretbox key.
    Identity doesn't currently store anything encrypted at rest.

If `AUTH_SIGNING_KEY` is unset, runs in `auth_disabled=True` mode (dev
/ standalone path). Production via the watchdog always supplies it.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthState:
    signing_key: bytes | None
    service_token: str | None
    master_key: bytes | None

    @property
    def auth_disabled(self) -> bool:
        return self.signing_key is None


def _decode_b64_key(value: str | None, *, expected_len: int, label: str) -> bytes | None:
    if not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as e:
        raise ValueError(f"{label}: not valid base64 ({e})") from e
    if len(raw) != expected_len:
        raise ValueError(
            f"{label}: expected {expected_len} bytes after base64-decode, got {len(raw)}"
        )
    return raw


def load_auth_state(
    *,
    signing_key_b64: str | None,
    service_token: str | None,
    master_key_b64: str | None,
) -> AuthState:
    signing_key = _decode_b64_key(signing_key_b64, expected_len=32, label="AUTH_SIGNING_KEY")
    master_key = _decode_b64_key(master_key_b64, expected_len=32, label="MASTER_KEY")

    if signing_key is None:
        if service_token or master_key:
            raise ValueError(
                "auth env vars inconsistent: SERVICE_TOKEN or MASTER_KEY is set but "
                "AUTH_SIGNING_KEY is not — refusing to start in a partially-auth state"
            )
        log.warning(
            "EUGENE_PLEXUS_IDENTITY_AUTH_SIGNING_KEY not set — running unauthenticated "
            "(dev/standalone mode). Production spawns via watchdog always supply this."
        )
        return AuthState(signing_key=None, service_token=None, master_key=None)

    return AuthState(
        signing_key=signing_key,
        service_token=service_token or None,
        master_key=master_key,
    )
