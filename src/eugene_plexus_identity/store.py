"""Storage layer for the identity component.

Two backing stores, both file-based and operator-inspectable:

  * **Constitution** — YAML file at `settings.constitution_file`.
    Loaded into memory at startup, written back on every successful
    PATCH. Default-empty installs get a minimal `name: Eugene`
    constitution so other components can always assemble a prompt.

  * **SQLite** — single file at `settings.db_file`. Holds persons,
    platform aliases (the alias graph), self-model entries, and
    pending identity links. One file, one connection, one threading
    lock — fits the personal-install scale; a future multi-writer
    backend swaps in behind the `IdentityStore` Protocol without
    touching route code.

The reflection write path (self-model entries from
`POST /v1/identity/self-model/reflect`) is in scope but returns 501
in this skeleton — actually generating reflections needs a configured
hemisphere-driver client, which lands in a follow-up commit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from ._generated.common_models import (
    Constitution,
    PendingIdentityLink,
    Person,
    PlatformAlias,
    SelfModelEntry,
    Status1,
)

log = logging.getLogger(__name__)

# Reserved platform / account for the operator's PlatformAlias entry.
# The wizard creates the operator's Person record with this alias so
# orchestrator can flag UI messages as "from operator" without a
# per-install special-case.
OPERATOR_PLATFORM = "ui"
OPERATOR_ACCOUNT_ID = "operator"


def _default_constitution() -> Constitution:
    """Empty install — Eugene knows only his name. Operator fills out
    the rest from the UI."""
    return Constitution(name="Eugene")


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Constitution
# --------------------------------------------------------------------------- #


class ConstitutionStore:
    """YAML-backed Constitution holder. Operator-editable from the UI.

    File shape (all keys optional except `name`):

        name: Eugene
        pronouns: he/him
        coreValues:
          - honesty
          - intellectual humility
        freeText: |
          Backstory in operator-supplied prose.

    Concurrent reads / writes go through `_lock`. The in-memory copy is
    authoritative — disk is the persistence backing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._current: Constitution = _default_constitution()

    def load(self) -> None:
        """Read from disk, falling back to defaults + writing them out
        if the file doesn't exist. Bad YAML raises so the lifespan
        can surface it before serving traffic."""
        with self._lock:
            if not self._path.exists():
                self._current = _default_constitution()
                self._write_locked()
                return
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(
                    f"{self._path} must be a YAML mapping at the root"
                )
            self._current = Constitution.model_validate(raw)

    def get(self) -> Constitution:
        with self._lock:
            return self._current.model_copy(deep=True)

    def update(self, patch: dict[str, Any]) -> Constitution:
        """Partial update. Unknown keys are dropped silently — the
        operator's UI is built off the schema so unknown keys only
        appear when someone PATCHes by hand with stale fields.
        Returns the new full Constitution."""
        with self._lock:
            current = self._current.model_dump(exclude_none=True)
            for k, v in patch.items():
                if v is None:
                    current.pop(k, None)
                else:
                    current[k] = v
            # Constitution.name is required; refuse to write an empty value.
            if not current.get("name"):
                raise ValueError("constitution.name must not be empty")
            self._current = Constitution.model_validate(current)
            self._write_locked()
            return self._current.model_copy(deep=True)

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._current.model_dump(exclude_none=True)
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=True, default_flow_style=False)


# --------------------------------------------------------------------------- #
# SQLite — persons, aliases, self-model, pending links
# --------------------------------------------------------------------------- #


_SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    person_id         TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL,
    is_operator       INTEGER NOT NULL DEFAULT 0,
    relationship_note TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_aliases (
    platform     TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    person_id    TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    handle       TEXT,
    display_name TEXT,
    avatar_url   TEXT,
    linked_at    TEXT NOT NULL,
    PRIMARY KEY (platform, account_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_by_person
    ON platform_aliases(person_id);

CREATE TABLE IF NOT EXISTS self_model_entries (
    id         TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_model_by_topic
    ON self_model_entries(topic);

CREATE TABLE IF NOT EXISTS self_model_persons (
    entry_id  TEXT NOT NULL REFERENCES self_model_entries(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, person_id)
);

CREATE TABLE IF NOT EXISTS pending_links (
    link_id            TEXT PRIMARY KEY,
    platform           TEXT NOT NULL,
    account_id         TEXT NOT NULL,
    display_name       TEXT,
    handle             TEXT,
    avatar_url         TEXT,
    first_seen         TEXT NOT NULL,
    triggering_message TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
    adapter_private    TEXT
);
-- Used to short-circuit duplicate adapter calls for an already-pending
-- (platform, accountId) pair.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_links_unique_pending
    ON pending_links(platform, account_id)
    WHERE status = 'pending';
"""


class IdentityStore:
    """SQLite-backed store. One file, one connection, one lock.

    All methods are thread-safe at the lock level. Each public method
    returns or accepts the spec's Pydantic shapes; row-to-model
    translation lives here so route code doesn't deal with SQL details.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ----- lifecycle --------------------------------------------------

    def load(self) -> None:
        """Open the DB connection and apply schema (idempotent).
        Called from the lifespan; never from route handlers."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because FastAPI's event loop may
        # schedule successive handlers on different threads; the
        # explicit threading.Lock around every operation guarantees
        # serial access regardless.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("IdentityStore.load() must be called first")
        return self._conn

    # ----- persons ----------------------------------------------------

    def list_persons(self) -> list[Person]:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT person_id, display_name, is_operator, relationship_note, created_at "
                "FROM persons ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
        return [self._row_to_person(row) for row in rows]

    def get_person(self, person_id: UUID) -> Person | None:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT person_id, display_name, is_operator, relationship_note, created_at "
                "FROM persons WHERE person_id = ?",
                (str(person_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_person(row)

    def create_person(
        self,
        *,
        display_name: str,
        relationship_note: str | None = None,
        is_operator: bool = False,
    ) -> Person:
        person_id = uuid4()
        created_at = _now()
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO persons (person_id, display_name, is_operator, "
                "relationship_note, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    str(person_id),
                    display_name,
                    1 if is_operator else 0,
                    relationship_note,
                    created_at.isoformat(),
                ),
            )
            conn.commit()
        return Person(
            personId=person_id,
            displayName=display_name,
            isOperator=is_operator,
            relationshipNote=relationship_note,
            createdAt=created_at,
            aliases=[],
        )

    def update_person(
        self,
        person_id: UUID,
        *,
        display_name: str | None = None,
        relationship_note: str | None = None,
    ) -> Person | None:
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT person_id, display_name, is_operator, relationship_note, created_at "
                "FROM persons WHERE person_id = ?",
                (str(person_id),),
            ).fetchone()
            if row is None:
                return None
            new_display = display_name if display_name is not None else row[1]
            new_note = relationship_note if relationship_note is not None else row[3]
            conn.execute(
                "UPDATE persons SET display_name = ?, relationship_note = ? "
                "WHERE person_id = ?",
                (new_display, new_note, str(person_id)),
            )
            conn.commit()
        return self.get_person(person_id)

    def delete_person(self, person_id: UUID) -> bool:
        """Returns True if a row was deleted. Refuses to delete the
        operator's record — caller (route) maps that to 409."""
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT is_operator FROM persons WHERE person_id = ?",
                (str(person_id),),
            ).fetchone()
            if row is None:
                return False
            if int(row[0]) == 1:
                raise PermissionError("refusing to delete the operator's person record")
            conn.execute("DELETE FROM persons WHERE person_id = ?", (str(person_id),))
            conn.commit()
        return True

    def list_aliases_for(self, person_id: UUID) -> list[PlatformAlias]:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT platform, account_id, handle, display_name, avatar_url, linked_at "
                "FROM platform_aliases WHERE person_id = ? ORDER BY linked_at ASC",
                (str(person_id),),
            )
            rows = cur.fetchall()
        return [self._row_to_alias(row) for row in rows]

    def add_alias(
        self,
        *,
        person_id: UUID,
        platform: str,
        account_id: str,
        handle: str | None,
        display_name: str | None,
        avatar_url: str | None,
    ) -> PlatformAlias:
        linked_at = _now()
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO platform_aliases (platform, account_id, person_id, "
                "handle, display_name, avatar_url, linked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    platform,
                    account_id,
                    str(person_id),
                    handle,
                    display_name,
                    avatar_url,
                    linked_at.isoformat(),
                ),
            )
            conn.commit()
        return PlatformAlias(
            platform=platform,
            accountId=account_id,
            handle=handle,
            displayName=display_name,
            avatarUrl=avatar_url,  # type: ignore[arg-type]
            linkedAt=linked_at,
        )

    def ensure_operator(self, *, display_name: str = "Operator") -> Person:
        """Idempotent: returns the operator's Person record, creating
        it (plus the canonical UI platform alias) on first call. The
        wizard calls this during initialize so identity has a known
        operator record before the first chat turn.
        """
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT person_id FROM persons WHERE is_operator = 1 LIMIT 1"
            )
            row = cur.fetchone()
            if row is not None:
                existing = self.get_person(UUID(row[0]))
                assert existing is not None
                return existing
        operator = self.create_person(display_name=display_name, is_operator=True)
        self.add_alias(
            person_id=operator.personId,
            platform=OPERATOR_PLATFORM,
            account_id=OPERATOR_ACCOUNT_ID,
            handle=None,
            display_name=display_name,
            avatar_url=None,
        )
        return operator

    # ----- self-model -------------------------------------------------

    def list_self_model(
        self,
        *,
        topic: str | None = None,
        person_id: UUID | None = None,
        limit: int = 5,
    ) -> list[SelfModelEntry]:
        """v0.2 ranking is simple: topic exact-match first (most
        recent first), then a recency-only sample to fill the limit.
        v0.3 plugs in semantic search."""
        results: list[SelfModelEntry] = []
        seen: set[str] = set()
        with self._lock:
            conn = self._require_conn()
            if topic:
                cur = conn.execute(
                    "SELECT id, topic, content, created_at FROM self_model_entries "
                    "WHERE topic = ? ORDER BY created_at DESC LIMIT ?",
                    (topic, limit),
                )
                for row in cur.fetchall():
                    eid = row[0]
                    seen.add(eid)
                    results.append(self._row_to_self_model(row, conn))
            if len(results) < limit:
                cur = conn.execute(
                    "SELECT id, topic, content, created_at FROM self_model_entries "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit * 2,),
                )
                for row in cur.fetchall():
                    if row[0] in seen:
                        continue
                    seen.add(row[0])
                    results.append(self._row_to_self_model(row, conn))
                    if len(results) >= limit:
                        break
        if person_id is None:
            return results
        # Filter by related person — done in-Python after pulling rather
        # than a JOIN because the result set is small and the filter is
        # cheap. If self-model ever grows huge this becomes a proper
        # subquery.
        filtered: list[SelfModelEntry] = []
        target = str(person_id)
        for entry in results:
            related = [str(p) for p in (entry.relatedPersonIds or [])]
            if target in related:
                filtered.append(entry)
        return filtered

    def insert_self_model(
        self,
        *,
        topic: str,
        content: str,
        related_person_ids: list[UUID] | None = None,
    ) -> SelfModelEntry:
        entry_id = uuid4()
        created_at = _now()
        related = related_person_ids or []
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO self_model_entries (id, topic, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (str(entry_id), topic, content, created_at.isoformat()),
            )
            for pid in related:
                conn.execute(
                    "INSERT OR IGNORE INTO self_model_persons (entry_id, person_id) "
                    "VALUES (?, ?)",
                    (str(entry_id), str(pid)),
                )
            conn.commit()
        return SelfModelEntry(
            id=entry_id,
            topic=topic,
            content=content,
            relatedPersonIds=list(related),
            createdAt=created_at,
        )

    # ----- pending links ----------------------------------------------

    def list_pending_links(self) -> list[PendingIdentityLink]:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT link_id, platform, account_id, display_name, handle, "
                "avatar_url, first_seen, triggering_message, status, adapter_private "
                "FROM pending_links ORDER BY first_seen DESC"
            )
            rows = cur.fetchall()
        return [self._row_to_pending_link(row) for row in rows]

    def get_pending_link(self, link_id: UUID) -> PendingIdentityLink | None:
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT link_id, platform, account_id, display_name, handle, "
                "avatar_url, first_seen, triggering_message, status, adapter_private "
                "FROM pending_links WHERE link_id = ?",
                (str(link_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_pending_link(row)

    def find_pending_for(
        self, platform: str, account_id: str
    ) -> PendingIdentityLink | None:
        """Returns the still-pending link for this (platform, account)
        pair, if one exists. Used to dedupe adapter calls."""
        with self._lock:
            cur = self._require_conn().execute(
                "SELECT link_id, platform, account_id, display_name, handle, "
                "avatar_url, first_seen, triggering_message, status, adapter_private "
                "FROM pending_links WHERE platform = ? AND account_id = ? AND status = 'pending'",
                (platform, account_id),
            )
            row = cur.fetchone()
        return self._row_to_pending_link(row) if row else None

    def create_pending_link(self, link: PendingIdentityLink) -> PendingIdentityLink:
        """Stores the link as `status='pending'` regardless of what the
        adapter sent in. linkId is server-assigned even when the adapter
        supplies one — keeping ID generation server-side prevents a
        misbehaving adapter from colliding with a previously-rejected
        record's PK. Only the approve/reject endpoints can move a link
        out of pending. Returns the stored record."""
        link_id = uuid4()
        first_seen = link.firstSeen or _now()
        # adapterPrivate is a free-form dict in the spec — datamodel-
        # code-generator produces it as a plain mapping rather than a
        # nested BaseModel, so just json-encode the dict directly.
        adapter_private_json = (
            json.dumps(link.adapterPrivate) if link.adapterPrivate is not None else None
        )
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "INSERT INTO pending_links (link_id, platform, account_id, display_name, "
                "handle, avatar_url, first_seen, triggering_message, status, adapter_private) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    str(link_id),
                    link.platform,
                    link.accountId,
                    link.displayName,
                    link.handle,
                    str(link.avatarUrl) if link.avatarUrl else None,
                    first_seen.isoformat(),
                    link.triggeringMessage,
                    adapter_private_json,
                ),
            )
            conn.commit()
        stored = self.get_pending_link(link_id)
        assert stored is not None
        return stored

    def update_link_status(
        self, link_id: UUID, new_status: Status1
    ) -> PendingIdentityLink | None:
        with self._lock:
            conn = self._require_conn()
            cur = conn.execute(
                "UPDATE pending_links SET status = ? WHERE link_id = ?",
                (new_status.value, str(link_id)),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_pending_link(link_id)

    # ----- row mappers ------------------------------------------------

    def _row_to_person(self, row: tuple[Any, ...]) -> Person:
        person_id = UUID(row[0])
        aliases = self.list_aliases_for(person_id)
        return Person(
            personId=person_id,
            displayName=row[1],
            isOperator=bool(row[2]),
            relationshipNote=row[3],
            createdAt=_parse_iso(row[4]),
            aliases=aliases,
        )

    def _row_to_alias(self, row: tuple[Any, ...]) -> PlatformAlias:
        return PlatformAlias(
            platform=row[0],
            accountId=row[1],
            handle=row[2],
            displayName=row[3],
            avatarUrl=row[4],
            linkedAt=_parse_iso(row[5]),
        )

    def _row_to_self_model(
        self, row: tuple[Any, ...], conn: sqlite3.Connection
    ) -> SelfModelEntry:
        entry_id = UUID(row[0])
        related_cur = conn.execute(
            "SELECT person_id FROM self_model_persons WHERE entry_id = ?",
            (str(entry_id),),
        )
        related = [UUID(r[0]) for r in related_cur.fetchall()]
        return SelfModelEntry(
            id=entry_id,
            topic=row[1],
            content=row[2],
            relatedPersonIds=related,
            createdAt=_parse_iso(row[3]),
        )

    def _row_to_pending_link(self, row: tuple[Any, ...]) -> PendingIdentityLink:
        adapter_private_raw = row[9]
        adapter_private = None
        if adapter_private_raw:
            try:
                adapter_private = json.loads(adapter_private_raw)
            except (TypeError, ValueError):
                log.warning("pending_link %s has unparseable adapter_private", row[0])
        return PendingIdentityLink(
            linkId=UUID(row[0]),
            platform=row[1],
            accountId=row[2],
            displayName=row[3],
            handle=row[4],
            avatarUrl=row[5],
            firstSeen=_parse_iso(row[6]),
            triggeringMessage=row[7],
            status=Status1(row[8]),
            adapterPrivate=adapter_private,
        )


def _parse_iso(raw: str) -> datetime:
    """Parse SQLite-stored ISO string back to an aware UTC datetime.

    Python's `fromisoformat` handles both the `+00:00` we write and a
    bare `Z` form some adapters may produce.
    """
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)
