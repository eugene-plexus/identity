"""Runtime configuration: standard Eugene Plexus config trio.

Identity has minimal runtime config in v0.2 — log level and a handful
of file paths that the operator can override post-install. The
constitution and SQLite paths default into `settings.constitution_file`
and `settings.db_file` (which themselves default into
`~/.eugene-plexus/identity/...`); operators relocating their install
can PATCH them here without restarting.

Sensitive values: none today. The infrastructure (REDACTED handling,
encrypted-at-rest envelopes via watchdog master key) is identical to
the hemisphere-driver pattern and lands here for free if a future
field needs it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from ._generated.common_models import (
    ComponentKind,
    ConfigDocument,
    ConfigField,
    ConfigFieldError,
    ConfigSchema,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    ConfigValueType,
)

CATEGORY_LABELS: dict[str, str] = {
    "logging": "Logging",
    "storage": "Storage",
    "reflection": "Self-Model Reflection",
}

FIELDS: list[ConfigField] = [
    ConfigField(
        key="logLevel",
        label="Log level",
        description=(
            "How chatty the identity component's terminal output is. "
            "`DEBUG` prints every store read and write; `INFO` is the "
            "normal operating level; `WARNING` and `ERROR` go "
            "progressively quieter."
        ),
        category="logging",
        valueType=ConfigValueType.enum,
        default="INFO",
        enumValues=["DEBUG", "INFO", "WARNING", "ERROR"],
        requiresRestart=True,
    ),
    ConfigField(
        key="reflectionHemisphereUrl",
        label="Reflection hemisphere",
        description=(
            "Which `hemisphere-driver` instance to call for the self-"
            "model reflection process. The UI populates this dropdown "
            "from the watchdog's topology; pick `(off)` to disable "
            "reflection (the endpoint returns 503 instead of trying)."
        ),
        category="reflection",
        valueType=ConfigValueType.url,
        # The wire value is still the peer's URL — the hint just tells
        # the UI to render this as a dropdown sourced from the watchdog
        # topology (filtered to hemisphere-driver instances) instead of
        # a free-text URL field the operator has to type by hand.
        componentKindHint=ComponentKind.hemisphere_driver,
        default=None,
        required=False,
    ),
    ConfigField(
        key="reflectionMemoryUrl",
        label="Reflection memory",
        description=(
            "Whether the reflection process can read recent turns from "
            "the `memory` component to ground its self-model updates. "
            "Pick `(off)` to disable memory-grounded reflection — the "
            "prompt will only include the constitution + existing self-"
            "model entries."
        ),
        category="reflection",
        valueType=ConfigValueType.url,
        # Stock topology has one memory backend, so this dropdown
        # collapses to effectively an on/off toggle. When v0.3+ adds
        # multiple backends, the same kindHint surfaces them all
        # without a UI rewrite — the dropdown just grows entries.
        componentKindHint=ComponentKind.memory,
        default=None,
        required=False,
    ),
    ConfigField(
        key="reflectionMaxLookbackTurns",
        label="Default reflection lookback turns",
        description=(
            "Default number of recent memory turns to include in the "
            "reflection prompt when the caller doesn't specify "
            "`lookbackTurns`. Higher gives Eugene more context to "
            "reflect on but eats more of the hemisphere's context "
            "window."
        ),
        category="reflection",
        valueType=ConfigValueType.integer,
        default=50,
        minimum=1,
        maximum=500,
    ),
]

_FIELDS_BY_KEY: dict[str, ConfigField] = {f.key: f for f in FIELDS}


def as_schema() -> ConfigSchema:
    return ConfigSchema(
        component="identity",
        fields=list(FIELDS),
        categories=CATEGORY_LABELS,
    )


def _defaults() -> dict[str, Any]:
    return {f.key: f.default for f in FIELDS if f.default is not None}


def _validate_value(field: ConfigField, value: Any) -> str | None:
    if value is None:
        return None
    vt = field.valueType
    if vt == ConfigValueType.enum:
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        allowed = field.enumValues or []
        if value not in allowed:
            return f"must be one of {allowed}"
        return None
    if vt in (ConfigValueType.string, ConfigValueType.url, ConfigValueType.file_path):
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        return None
    if vt == ConfigValueType.integer:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"expected integer, got {type(value).__name__}"
        if field.minimum is not None and value < field.minimum:
            return f"must be >= {field.minimum}"
        if field.maximum is not None and value > field.maximum:
            return f"must be <= {field.maximum}"
        return None
    return f"unsupported valueType: {vt}"


class ConfigStore:
    """File-backed config store. Thread-safe single-writer model."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._values: dict[str, Any] = _defaults()

    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{self._path} must be a YAML mapping at the root"
                    )
                merged = _defaults()
                for k, v in raw.items():
                    if k in _FIELDS_BY_KEY:
                        merged[k] = v
                self._values = merged
            else:
                self._values = _defaults()
                self._write_locked()

    def as_document(self) -> ConfigDocument:
        with self._lock:
            return ConfigDocument.model_validate(dict(self._values))

    def apply_patch(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        applied: list[str] = []
        rejected: list[ConfigFieldError] = []
        pending_restart: list[str] = []
        patch: dict[str, Any] = request.model_dump()

        with self._lock:
            for key, new_value in patch.items():
                field = _FIELDS_BY_KEY.get(key)
                if field is None:
                    rejected.append(ConfigFieldError(key=key, message="unknown field"))
                    continue
                err = _validate_value(field, new_value)
                if err is not None:
                    rejected.append(ConfigFieldError(key=key, message=err))
                    continue
                if new_value is None and field.default is not None:
                    self._values[key] = field.default
                else:
                    self._values[key] = new_value
                applied.append(key)
                if field.requiresRestart:
                    pending_restart.append(key)
            if applied:
                self._write_locked()
            return ConfigUpdateResult(
                applied=applied,
                rejected=rejected,
                requiresRestart=bool(pending_restart),
                pendingRestart=sorted(pending_restart),
            )

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values.get(key)

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(self._values, f, sort_keys=True, default_flow_style=False)
