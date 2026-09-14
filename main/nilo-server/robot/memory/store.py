"""Where memory lives: an interface, a SQLite implementation, and its migrations.

    store = SqliteMemoryStore("data/robot_memory.sqlite3")
    await store.open()                       # runs migrations, idempotently
    await store.add_episode(episode)
    episodes = await store.recent_episodes("nilo-sim-01", limit=20)

The interface (:class:`MemoryStore`) is the boundary, and it is written so a Postgres
implementation is a new class rather than a rewrite: every method is a coroutine, every
argument and return value is a pydantic model or a primitive, there is no cursor, no
session and no lazy relationship anywhere in it, and the only compound operation
(``upsert_fact``) is expressed as read-modify-write under one lock — which maps onto
``INSERT ... ON CONFLICT DO UPDATE`` when the backend has it.

**Why stdlib ``sqlite3`` rather than an ORM.** The test suite runs against
``requirements-dev.txt`` alone (see ``.github/workflows/test.yml``), so a new runtime
dependency would either break that job or make robot memory untestable in it. What an ORM
would buy here is migrations and dialect portability; the migration list below is twenty
lines, and the dialect portability is exactly what the interface above already provides.
The cost of the ORM would be paid by every deployment; the cost of this is paid once, here.

**Blocking work goes to a thread.** ``sqlite3`` is synchronous, and the session event loop
runs ONNX inference and blocking HTTP (docs/robot-architecture.md R3). Every query in this
module runs through ``asyncio.to_thread``, and the connection is opened with
``check_same_thread=False`` plus one lock, which is what makes that safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from robot.memory.models import (
    EpisodicMemory,
    EventType,
    PersonRecord,
    Provenance,
    SemanticFact,
)
from robot.state.models import utcnow

logger = logging.getLogger(__name__)

#: Where the memory database lives when no path is given, relative to the server directory.
DEFAULT_DB_PATH = Path("data") / "robot_memory.sqlite3"

#: ``:memory:`` is a real SQLite path and the right one for a test.
IN_MEMORY = ":memory:"


class MemoryStore(ABC):
    """The persistence boundary for robot memory.

    Narrow on purpose. Everything above this knows about episodes, facts and people;
    nothing above it knows about SQL, connections or transactions.
    """

    # -- lifecycle ------------------------------------------------------------------------

    @abstractmethod
    async def open(self) -> None:
        """Connect and bring the schema up to date. Idempotent."""

    @abstractmethod
    async def aclose(self) -> None:
        """Flush and disconnect. Idempotent."""

    # -- episodic -------------------------------------------------------------------------

    @abstractmethod
    async def add_episode(self, episode: EpisodicMemory) -> EpisodicMemory: ...

    @abstractmethod
    async def get_episode(self, episode_id: str) -> EpisodicMemory | None: ...

    @abstractmethod
    async def recent_episodes(
        self,
        robot_id: str,
        *,
        limit: int = 20,
        person_id: str | None = None,
        event_types: Sequence[EventType] = (),
        since: datetime | None = None,
        min_importance: float = 0.0,
        unconsolidated_only: bool = False,
    ) -> list[EpisodicMemory]: ...

    @abstractmethod
    async def mark_consolidated(self, episode_ids: Sequence[str], *, now: datetime | None = None) -> int: ...

    @abstractmethod
    async def delete_episode(self, episode_id: str) -> bool: ...

    # -- semantic -------------------------------------------------------------------------

    @abstractmethod
    async def upsert_fact(self, fact: SemanticFact) -> SemanticFact:
        """Write a fact, merging with an existing one for the same subject and predicate.

        Never a blind overwrite: an implementation must apply
        :func:`merge_fact`, which keeps the higher-confidence value and records the
        confirmation rather than replacing the provenance.
        """

    @abstractmethod
    async def get_fact(self, robot_id: str, subject: str, predicate: str) -> SemanticFact | None: ...

    @abstractmethod
    async def facts(
        self, robot_id: str, *, subject: str | None = None, limit: int = 100
    ) -> list[SemanticFact]: ...

    @abstractmethod
    async def delete_fact(self, fact_id: str) -> bool: ...

    # -- people ---------------------------------------------------------------------------

    @abstractmethod
    async def upsert_person(self, person: PersonRecord) -> PersonRecord: ...

    @abstractmethod
    async def get_person(self, robot_id: str, person_id: str) -> PersonRecord | None: ...

    @abstractmethod
    async def people(self, robot_id: str, *, limit: int = 100) -> list[PersonRecord]: ...

    @abstractmethod
    async def delete_person(self, robot_id: str, person_id: str) -> int:
        """Delete a person **and everything about them**. Returns the number of rows removed.

        The privacy operation: the person record, their episodes and every fact whose
        subject is them. A "delete" that leaves the episodes behind is not a delete.
        """

    # -- whole-robot ----------------------------------------------------------------------

    @abstractmethod
    async def clear_robot(self, robot_id: str) -> int:
        """Forget everything about one robot. Returns the number of rows removed."""

    @abstractmethod
    async def counts(self, robot_id: str) -> dict[str, int]:
        """How much is stored, per kind. For the API and for a log line."""


def merge_fact(existing: SemanticFact, incoming: SemanticFact, *, now: datetime | None = None) -> SemanticFact:
    """Combine a new claim with a stored one. The rule that stops blind overwrites.

    * **Same value** — a confirmation. Confidence rises towards 1 (never reaching it), the
      provenance records the confirmation and gains the new source ids.
    * **Different value, higher confidence** — the new value wins, and the provenance keeps
      the original ``created_at`` so the history is not rewritten.
    * **Different value, not higher confidence** — the stored value stands, and the
      attempt is recorded in ``metadata['disputed']``. A model that is less sure than
      what the robot already knows does not get to change the answer
      (docs/robot-memory.md).
    """
    moment = now or utcnow()
    if existing.value == incoming.value:
        confidence = existing.confidence + (1.0 - existing.confidence) * 0.25
        return existing.model_copy(
            update={
                "confidence": min(1.0, round(confidence, 4)),
                "provenance": existing.provenance.confirmed(
                    now=moment, source_ids=incoming.provenance.source_ids
                ),
            }
        )
    if incoming.confidence > existing.confidence:
        provenance = incoming.provenance.model_copy(
            update={"created_at": existing.provenance.created_at, "updated_at": moment}
        )
        metadata = dict(existing.metadata)
        metadata["previous_value"] = existing.value
        return incoming.model_copy(
            update={"id": existing.id, "provenance": provenance, "metadata": metadata}
        )
    metadata = dict(existing.metadata)
    disputed = list(metadata.get("disputed", []))
    disputed.append(
        {"value": incoming.value, "confidence": incoming.confidence, "at": moment.isoformat()}
    )
    metadata["disputed"] = disputed[-5:]  # bounded: a loop of bad claims is not a log file
    return existing.model_copy(
        update={"metadata": metadata, "provenance": existing.provenance.model_copy(update={"updated_at": moment})}
    )


# -- the SQLite implementation ---------------------------------------------------------------


#: Ordered schema migrations. Each entry is applied once, in order, inside a transaction,
#: and the applied version is recorded in ``schema_version``. Adding a migration is adding
#: a tuple; never edit one that has shipped.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            robot_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL,
            person_id TEXT,
            importance REAL NOT NULL DEFAULT 0.4,
            metadata TEXT NOT NULL DEFAULT '{}',
            consolidated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS episodes_robot_time ON episodes (robot_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS episodes_person ON episodes (robot_id, person_id);

        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            robot_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.6,
            learned_from TEXT NOT NULL DEFAULT 'unknown',
            source_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            confirmations INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}',
            UNIQUE (robot_id, subject, predicate)
        );
        CREATE INDEX IF NOT EXISTS facts_subject ON facts (robot_id, subject);

        CREATE TABLE IF NOT EXISTS people (
            robot_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            interaction_count INTEGER NOT NULL DEFAULT 0,
            familiarity REAL NOT NULL DEFAULT 0.0,
            embedding_ref TEXT,
            facts TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (robot_id, person_id)
        );
        """,
    ),
    (
        2,
        """
        -- Optional vector search. The column is nullable and every query works without it,
        -- which is what "a robot must function with embeddings disabled" means in schema
        -- terms (docs/robot-memory.md).
        ALTER TABLE episodes ADD COLUMN embedding TEXT;
        """,
    ),
)


class SqliteMemoryStore(MemoryStore):
    """SQLite, through one connection and one lock, with every query off the event loop."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------------------------

    async def open(self) -> None:
        if self._connection is not None:
            return
        if self.path != IN_MEMORY:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = await asyncio.to_thread(self._connect)
        self._connection = connection
        await asyncio.to_thread(self._migrate, connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL so a reader (the API) and a writer (the robot) do not block each other;
        # foreign keys on so a future schema can rely on them.
        if self.path != IN_MEMORY:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version").fetchone()
        current = int(row["version"])
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            with connection:  # one transaction per migration
                connection.executescript(script)
                connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            logger.info("robot memory: applied schema migration %d", version)

    async def aclose(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await asyncio.to_thread(connection.close)

    @property
    def connected(self) -> bool:
        return self._connection is not None

    async def schema_version(self) -> int:
        rows = await self._query("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version")
        return int(rows[0]["version"]) if rows else 0

    # -- episodic ---------------------------------------------------------------------------

    async def add_episode(self, episode: EpisodicMemory) -> EpisodicMemory:
        await self._execute(
            """
            INSERT OR REPLACE INTO episodes
                (id, robot_id, event_type, summary, timestamp, person_id, importance, metadata, consolidated_at, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.id,
                episode.robot_id,
                episode.event_type.value,
                episode.summary,
                episode.timestamp.isoformat(),
                episode.person_id,
                episode.importance,
                json.dumps(episode.metadata),
                episode.consolidated_at.isoformat() if episode.consolidated_at else None,
                json.dumps(episode.metadata.get("embedding")) if episode.metadata.get("embedding") else None,
            ),
        )
        return episode

    async def get_episode(self, episode_id: str) -> EpisodicMemory | None:
        rows = await self._query("SELECT * FROM episodes WHERE id = ?", (episode_id,))
        return _episode(rows[0]) if rows else None

    async def recent_episodes(
        self,
        robot_id: str,
        *,
        limit: int = 20,
        person_id: str | None = None,
        event_types: Sequence[EventType] = (),
        since: datetime | None = None,
        min_importance: float = 0.0,
        unconsolidated_only: bool = False,
    ) -> list[EpisodicMemory]:
        sql = ["SELECT * FROM episodes WHERE robot_id = ?"]
        args: list[Any] = [robot_id]
        if person_id is not None:
            sql.append("AND person_id = ?")
            args.append(person_id)
        if event_types:
            sql.append(f"AND event_type IN ({', '.join('?' * len(event_types))})")
            args.extend(kind.value for kind in event_types)
        if since is not None:
            sql.append("AND timestamp >= ?")
            args.append(since.isoformat())
        if min_importance > 0:
            sql.append("AND importance >= ?")
            args.append(min_importance)
        if unconsolidated_only:
            sql.append("AND consolidated_at IS NULL")
        sql.append("ORDER BY timestamp DESC LIMIT ?")
        args.append(limit)
        rows = await self._query(" ".join(sql), tuple(args))
        return [_episode(row) for row in rows]

    async def mark_consolidated(self, episode_ids: Sequence[str], *, now: datetime | None = None) -> int:
        if not episode_ids:
            return 0
        moment = (now or utcnow()).isoformat()
        placeholders = ", ".join("?" * len(episode_ids))
        return await self._execute(
            f"UPDATE episodes SET consolidated_at = ? WHERE id IN ({placeholders})",
            (moment, *episode_ids),
        )

    async def delete_episode(self, episode_id: str) -> bool:
        return await self._execute("DELETE FROM episodes WHERE id = ?", (episode_id,)) > 0

    # -- semantic ---------------------------------------------------------------------------

    async def upsert_fact(self, fact: SemanticFact) -> SemanticFact:
        """Merge under the lock, so two writers cannot both read-then-overwrite."""
        async with self._lock:
            existing = await self._get_fact_unlocked(fact.robot_id, fact.subject, fact.predicate)
            merged = fact if existing is None else merge_fact(existing, fact)
            await self._execute(
                """
                INSERT INTO facts
                    (id, robot_id, subject, predicate, value, confidence, learned_from,
                     source_ids, created_at, updated_at, confirmations, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (robot_id, subject, predicate) DO UPDATE SET
                    value = excluded.value,
                    confidence = excluded.confidence,
                    learned_from = excluded.learned_from,
                    source_ids = excluded.source_ids,
                    updated_at = excluded.updated_at,
                    confirmations = excluded.confirmations,
                    metadata = excluded.metadata
                """,
                (
                    merged.id,
                    merged.robot_id,
                    merged.subject,
                    merged.predicate,
                    merged.value,
                    merged.confidence,
                    merged.provenance.learned_from,
                    json.dumps(list(merged.provenance.source_ids)),
                    merged.provenance.created_at.isoformat(),
                    merged.provenance.updated_at.isoformat(),
                    merged.provenance.confirmations,
                    json.dumps(merged.metadata),
                ),
            )
            return merged

    async def get_fact(self, robot_id: str, subject: str, predicate: str) -> SemanticFact | None:
        return await self._get_fact_unlocked(robot_id, subject, predicate)

    async def _get_fact_unlocked(self, robot_id: str, subject: str, predicate: str) -> SemanticFact | None:
        rows = await self._query(
            "SELECT * FROM facts WHERE robot_id = ? AND subject = ? AND predicate = ?",
            (robot_id, subject, predicate),
        )
        return _fact(rows[0]) if rows else None

    async def facts(self, robot_id: str, *, subject: str | None = None, limit: int = 100) -> list[SemanticFact]:
        if subject is None:
            rows = await self._query(
                "SELECT * FROM facts WHERE robot_id = ? ORDER BY confidence DESC, subject LIMIT ?",
                (robot_id, limit),
            )
        else:
            rows = await self._query(
                "SELECT * FROM facts WHERE robot_id = ? AND subject = ? ORDER BY confidence DESC LIMIT ?",
                (robot_id, subject, limit),
            )
        return [_fact(row) for row in rows]

    async def delete_fact(self, fact_id: str) -> bool:
        return await self._execute("DELETE FROM facts WHERE id = ?", (fact_id,)) > 0

    # -- people -----------------------------------------------------------------------------

    async def upsert_person(self, person: PersonRecord) -> PersonRecord:
        await self._execute(
            """
            INSERT INTO people
                (robot_id, person_id, display_name, first_seen, last_seen,
                 interaction_count, familiarity, embedding_ref, facts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (robot_id, person_id) DO UPDATE SET
                display_name = excluded.display_name,
                last_seen = excluded.last_seen,
                interaction_count = excluded.interaction_count,
                familiarity = excluded.familiarity,
                embedding_ref = excluded.embedding_ref,
                facts = excluded.facts
            """,
            (
                person.robot_id,
                person.person_id,
                person.display_name,
                person.first_seen.isoformat(),
                person.last_seen.isoformat(),
                person.interaction_count,
                person.familiarity,
                person.embedding_ref,
                json.dumps(person.facts),
            ),
        )
        return person

    async def get_person(self, robot_id: str, person_id: str) -> PersonRecord | None:
        rows = await self._query(
            "SELECT * FROM people WHERE robot_id = ? AND person_id = ?", (robot_id, person_id)
        )
        return _person(rows[0]) if rows else None

    async def people(self, robot_id: str, *, limit: int = 100) -> list[PersonRecord]:
        rows = await self._query(
            "SELECT * FROM people WHERE robot_id = ? ORDER BY last_seen DESC LIMIT ?", (robot_id, limit)
        )
        return [_person(row) for row in rows]

    async def delete_person(self, robot_id: str, person_id: str) -> int:
        removed = 0
        removed += await self._execute(
            "DELETE FROM people WHERE robot_id = ? AND person_id = ?", (robot_id, person_id)
        )
        removed += await self._execute(
            "DELETE FROM episodes WHERE robot_id = ? AND person_id = ?", (robot_id, person_id)
        )
        removed += await self._execute(
            "DELETE FROM facts WHERE robot_id = ? AND subject = ?", (robot_id, person_id)
        )
        return removed

    # -- whole-robot -------------------------------------------------------------------------

    async def clear_robot(self, robot_id: str) -> int:
        removed = 0
        for table in ("episodes", "facts", "people"):
            removed += await self._execute(f"DELETE FROM {table} WHERE robot_id = ?", (robot_id,))
        return removed

    async def counts(self, robot_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in ("episodes", "facts", "people"):
            rows = await self._query(f"SELECT COUNT(*) AS n FROM {table} WHERE robot_id = ?", (robot_id,))
            counts[table] = int(rows[0]["n"]) if rows else 0
        return counts

    async def robot_ids(self) -> list[str]:
        rows = await self._query(
            "SELECT DISTINCT robot_id FROM episodes UNION SELECT DISTINCT robot_id FROM people"
        )
        return sorted(str(row["robot_id"]) for row in rows)

    # -- plumbing ------------------------------------------------------------------------------

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("the memory store is not open; call await store.open() first")
        return self._connection

    async def _query(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = self._require()
        return await asyncio.to_thread(lambda: connection.execute(sql, args).fetchall())

    async def _execute(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        connection = self._require()

        def run() -> int:
            with connection:
                cursor = connection.execute(sql, args)
                return int(cursor.rowcount)

        return await asyncio.to_thread(run)

    def __repr__(self) -> str:
        return f"<SqliteMemoryStore {self.path} {'open' if self.connected else 'closed'}>"


# -- row mapping ---------------------------------------------------------------------------------


def _episode(row: sqlite3.Row) -> EpisodicMemory:
    return EpisodicMemory(
        id=str(row["id"]),
        robot_id=str(row["robot_id"]),
        event_type=EventType(str(row["event_type"])),
        summary=str(row["summary"]),
        timestamp=datetime.fromisoformat(str(row["timestamp"])),
        person_id=row["person_id"],
        importance=float(row["importance"]),
        metadata=json.loads(str(row["metadata"] or "{}")),
        consolidated_at=(
            datetime.fromisoformat(str(row["consolidated_at"])) if row["consolidated_at"] else None
        ),
    )


def _fact(row: sqlite3.Row) -> SemanticFact:
    return SemanticFact(
        id=str(row["id"]),
        robot_id=str(row["robot_id"]),
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        value=str(row["value"]),
        confidence=float(row["confidence"]),
        provenance=Provenance(
            learned_from=str(row["learned_from"]),
            source_ids=tuple(json.loads(str(row["source_ids"] or "[]"))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            confirmations=int(row["confirmations"]),
        ),
        metadata=json.loads(str(row["metadata"] or "{}")),
    )


def _person(row: sqlite3.Row) -> PersonRecord:
    return PersonRecord(
        person_id=str(row["person_id"]),
        robot_id=str(row["robot_id"]),
        display_name=str(row["display_name"]),
        first_seen=datetime.fromisoformat(str(row["first_seen"])),
        last_seen=datetime.fromisoformat(str(row["last_seen"])),
        interaction_count=int(row["interaction_count"]),
        familiarity=float(row["familiarity"]),
        embedding_ref=row["embedding_ref"],
        facts=json.loads(str(row["facts"] or "{}")),
    )


def episode_texts(episodes: Iterable[EpisodicMemory]) -> list[str]:
    return [episode.text for episode in episodes]


__all__ = [
    "DEFAULT_DB_PATH",
    "IN_MEMORY",
    "MIGRATIONS",
    "MemoryStore",
    "SqliteMemoryStore",
    "episode_texts",
    "merge_fact",
]
