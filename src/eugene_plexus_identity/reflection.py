"""Self-model reflection service.

Eugene's PCC/precuneus analogue: looks back at recent conversation
turns, asks a configured hemisphere-driver to extract autobiographical
observations ("I tend to..", "I notice myself..."), and persists those
as `SelfModelEntry` rows in identity's SQLite store.

v0.2 ships the manual-trigger path only — `POST /v1/identity/self-
model/reflect`. v0.3 ties this to NT idle states (high serotonin +
low cortisol → autonomous reflection) so Eugene mind-wanders on
his own.

The reflection prompt asks the hemisphere to respond in a structured
JSON shape — robust enough for modern frontier models and reliable
enough for a manual-trigger feature. Free-form text would need a
parser; JSON is good enough.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from uuid import UUID

import httpx

from ._generated.common_models import SelfModelEntry
from .clients import HemisphereClient, MemoryClient
from .clients.memory_client import MemoryMessage
from .store import ConstitutionStore, IdentityStore

log = logging.getLogger(__name__)


class ReflectionConfigError(Exception):
    """Raised when reflection is invoked but the operator hasn't
    configured a hemisphere URL. The route maps this to 503."""

    def __init__(self, *, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


REFLECTION_SYSTEM_PROMPT = (
    "You are Eugene's self-reflection process. Your job is to look at "
    "Eugene's recent conversations and write short autobiographical "
    "observations — patterns Eugene is starting to notice about "
    "himself, tendencies he's developing, how he tends to engage with "
    "specific topics or people.\n\n"
    "Output EXACTLY a JSON object with this shape:\n"
    '{"entries": [{"topic": "short-topic-key", "content": '
    '"one or two sentence first-person observation about yourself"}]}\n\n'
    "Constraints:\n"
    "- `topic` is a short hyphenated kebab-case key for retrieval "
    '(examples: "creative-tasks", "uncertainty-handling", '
    '"user-troy", "explanation-style").\n'
    '- `content` is FIRST PERSON. Write "I notice...", "I tend '
    'to...", "With Troy I usually...", not third-person about '
    "Eugene.\n"
    "- Return 0-5 entries. Quality over quantity. If nothing new "
    "stands out, return an empty entries list.\n"
    "- Output ONLY the JSON object. No prose, no markdown fences, no "
    "explanation."
)


def _format_turns_for_prompt(turns: list[MemoryMessage]) -> str:
    """Render memory turns into a compact transcript for the prompt.

    Each turn becomes one line: `<role>: <content>`. Long content is
    truncated to keep the prompt bounded — the hemisphere doesn't
    need full essays to extract patterns, just enough flavor.
    """
    lines: list[str] = []
    for turn in turns:
        content = turn.content
        if len(content) > 400:
            content = content[:400] + "…"
        lines.append(f"{turn.role}: {content}")
    return "\n".join(lines)


def _format_existing_self_model(entries: list[SelfModelEntry]) -> str:
    if not entries:
        return "(no prior self-model entries)"
    lines = []
    for e in entries:
        content = e.content
        if len(content) > 240:
            content = content[:240] + "…"
        lines.append(f"- [{e.topic}] {content}")
    return "\n".join(lines)


def build_reflection_user_message(
    *,
    constitution_yaml: str,
    existing_entries: list[SelfModelEntry],
    recent_turns: list[MemoryMessage],
) -> str:
    """Assemble the operator-facing-content half of the reflection
    prompt. Separated from `REFLECTION_SYSTEM_PROMPT` so tests can
    inspect the full input cheaply."""
    parts: list[str] = []
    parts.append("== Eugene's constitution (declarative facts about who you are) ==")
    parts.append(constitution_yaml.strip())
    parts.append("")
    parts.append("== Existing self-model entries (patterns you've noticed before) ==")
    parts.append(_format_existing_self_model(existing_entries))
    parts.append("")
    if recent_turns:
        parts.append("== Recent conversation turns to reflect on ==")
        parts.append(_format_turns_for_prompt(recent_turns))
    else:
        parts.append("== Recent conversation turns ==")
        parts.append(
            "(no recent turns supplied; reflect on the constitution + existing entries alone)"
        )
    parts.append("")
    parts.append(
        "Now produce the JSON object described in your system message. "
        "Look for genuinely new observations — don't restate existing "
        "entries verbatim."
    )
    return "\n".join(parts)


# Pulls a JSON object out of a response that may contain markdown code
# fences or surrounding prose. Falls back to the raw string if no
# fenced block is found.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_reflection_response(text: str) -> list[tuple[str, str]]:
    """Parse the hemisphere's reflection output into `(topic, content)`
    tuples. Tolerant of:
      - markdown code fences (``` or ```json)
      - leading / trailing prose around the JSON
      - missing `entries` key (returns empty)
      - non-string topic / content fields (skipped silently)

    Returns an empty list on any parse failure; the route surfaces
    that as "reflection produced no entries" rather than 5xx.
    """
    cleaned = text.strip()
    fenced = _JSON_FENCE_RE.search(cleaned)
    if fenced is not None:
        cleaned = fenced.group(1)
    # Find the first { and last } if the model wrapped the JSON in prose.
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        return []
    payload = cleaned[first_brace : last_brace + 1]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as e:
        log.warning("reflection response was not valid JSON: %s", e)
        return []
    if not isinstance(parsed, dict):
        return []
    raw_entries = parsed.get("entries")
    if not isinstance(raw_entries, list):
        return []
    out: list[tuple[str, str]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        topic = raw.get("topic")
        content = raw.get("content")
        if not isinstance(topic, str) or not isinstance(content, str):
            continue
        topic = topic.strip()
        content = content.strip()
        if not topic or not content:
            continue
        out.append((topic, content))
    return out


@dataclass(frozen=True)
class ReflectionResult:
    entries_written: list[SelfModelEntry]


async def run_reflection(
    *,
    constitution_store: ConstitutionStore,
    identity_store: IdentityStore,
    hemisphere_client: HemisphereClient | None,
    memory_client: MemoryClient | None,
    lookback_turns: int,
    conversation_id: UUID | None,
    related_person_id: UUID | None,
) -> ReflectionResult:
    """Run the reflection flow end-to-end.

    Raises `ReflectionConfigError` when the hemisphere client isn't
    configured. Other failures (memory unreachable, hemisphere returns
    a non-2xx) propagate as `httpx.HTTPError` / `HemisphereError` —
    the route maps these to 503 with the upstream detail.
    """
    if hemisphere_client is None:
        raise ReflectionConfigError(
            detail=(
                "Reflection is unavailable: `reflectionHemisphereUrl` is "
                "not configured. Set it via PATCH /v1/config to enable."
            )
        )

    # Gather recent turns. Three paths:
    #   - explicit conversationId → that conversation's full message log
    #   - memory client + related_person_id → that person's recent turns
    #   - no memory client OR no person → empty (constitution-only reflection)
    recent_turns: list[MemoryMessage] = []
    if memory_client is not None:
        try:
            if conversation_id is not None:
                recent_turns = await memory_client.conversation(conversation_id)
            elif related_person_id is not None:
                recent_turns = await memory_client.recent_for_person(
                    person_id=related_person_id, limit=lookback_turns
                )
        except httpx.HTTPError as e:
            log.warning(
                "memory unreachable during reflection: %s — proceeding "
                "with constitution + existing entries only",
                e,
            )
            recent_turns = []

    # Cap by the caller's lookback even on the conversation path so a
    # long-running thread doesn't blow the hemisphere's context window.
    if len(recent_turns) > lookback_turns:
        recent_turns = recent_turns[-lookback_turns:]

    # Build the prompt.
    constitution = constitution_store.get()
    import yaml

    constitution_yaml = yaml.safe_dump(
        constitution.model_dump(exclude_none=True),
        sort_keys=True,
        default_flow_style=False,
    )
    existing_entries = identity_store.list_self_model(topic=None, person_id=None, limit=20)
    user_message = build_reflection_user_message(
        constitution_yaml=constitution_yaml,
        existing_entries=existing_entries,
        recent_turns=recent_turns,
    )

    # Call the hemisphere. HemisphereError propagates as-is for the
    # route to surface; other transport errors also propagate.
    response_text = await hemisphere_client.generate(
        system_prompt=REFLECTION_SYSTEM_PROMPT,
        user_message=user_message,
    )

    # Parse and persist.
    parsed = parse_reflection_response(response_text)
    related = [related_person_id] if related_person_id is not None else None
    written: list[SelfModelEntry] = []
    for topic, content in parsed:
        entry = identity_store.insert_self_model(
            topic=topic, content=content, related_person_ids=related
        )
        written.append(entry)
    log.info(
        "reflection produced %d new self-model entries (from %d raw)",
        len(written),
        len(parsed),
    )
    return ReflectionResult(entries_written=written)


__all__ = [
    "ReflectionConfigError",
    "ReflectionResult",
    "build_reflection_user_message",
    "parse_reflection_response",
    "run_reflection",
]
