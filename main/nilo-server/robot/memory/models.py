"""What the robot remembers, in four shapes — because it is four different things.

The design rule this file exists to enforce: **not everything is a vector**. An embedding
is one retrieval strategy, and a poor one for "when did I last see Ahmad" (a timestamp
query), "what is my name" (a fact), or "what are we talking about right now" (a buffer
that should be thrown away shortly).

So:

``EpisodicMemory``  things that happened, with a time and an importance
``SemanticFact``    stable learned facts, with provenance and a confidence
``PersonRecord``    who somebody is, and how well the robot knows them
``WorkingMemory``   the current conversation, bounded and short-lived (``working.py``)

Everything here is a frozen pydantic model with no dependency beyond pydantic, so a
record can be stored in SQLite today and Postgres tomorrow without touching this file, and
asserted on in a test with no database at all.

**Provenance is not optional.** Every semantic fact carries where it came from, when it
was created, when it was last updated, and how confident the robot is. A fact with no
provenance cannot be audited, corrected, or told apart from something a model invented —
which is why nothing is allowed to write one without it (docs/robot-memory.md).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from robot.state.models import utcnow


class MemoryKind(str, Enum):
    """Which store a record belongs to. Used by the API and the delete paths."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PERSON = "person"
    WORKING = "working"


class EventType(str, Enum):
    """What kind of thing happened. Closed, so a query can group by it.

    Deliberately short. "met Ahmad", "played with the cube", "failed to reach the charger"
    are all one of these plus a summary, and a taxonomy with fifty entries is one nobody
    uses consistently.
    """

    MET_PERSON = "met_person"
    CONVERSATION = "conversation"
    PLAY = "play"
    PRAISE = "praise"
    SCOLD = "scold"
    COMMAND = "command"
    OBSERVATION = "observation"
    NAVIGATION = "navigation"
    FAILURE = "failure"
    CHARGE = "charge"
    SYSTEM = "system"


#: Importance is 0..1. These are the anchors the code and the docs use, so "important"
#: means the same thing in a consolidation rule and in a retrieval budget.
IMPORTANCE_TRIVIAL = 0.1
IMPORTANCE_NORMAL = 0.4
IMPORTANCE_NOTABLE = 0.7
IMPORTANCE_CRITICAL = 0.9


class Provenance(BaseModel):
    """Where a remembered fact came from. Attached to every semantic fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What produced it: ``consolidation``, ``user``, ``operator``, ``vision``, ``llm``…
    learned_from: str = "unknown"
    #: The episodic memory ids it was derived from, when it was derived from any.
    source_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: How many times something has confirmed this fact. Confirmation raises confidence;
    #: it never rewrites the original.
    confirmations: int = 0

    def confirmed(self, *, now: datetime | None = None, source_ids: tuple[str, ...] = ()) -> Provenance:
        return self.model_copy(
            update={
                "updated_at": now or utcnow(),
                "confirmations": self.confirmations + 1,
                "source_ids": tuple(dict.fromkeys(self.source_ids + source_ids)),
            }
        )


class EpisodicMemory(BaseModel):
    """One thing that happened, at a time, with a weight.

    ``summary`` is a sentence a human could read. It is not a transcript: episodic memory
    is what a robot recalls about an event, and storing every word of every turn is what
    makes a memory store unusable rather than useful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    robot_id: str
    event_type: EventType = EventType.OBSERVATION
    summary: str = ""
    timestamp: datetime = Field(default_factory=utcnow)
    person_id: str | None = None
    importance: float = Field(default=IMPORTANCE_NORMAL, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Set once this episode has been folded into a semantic fact, so consolidation is
    #: idempotent and a restart does not re-derive everything.
    consolidated_at: datetime | None = None

    @field_validator("robot_id")
    @classmethod
    def _robot_id_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an episodic memory must belong to a robot")
        return value

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.timestamp).total_seconds()

    @property
    def text(self) -> str:
        """What retrieval puts in a prompt. One line, with the person named."""
        who = f" [{self.person_id}]" if self.person_id else ""
        return f"{self.event_type.value}{who}: {self.summary}".strip()


class SemanticFact(BaseModel):
    """Something the robot believes to be true, with a record of why.

    ``subject`` plus ``predicate`` is the identity: writing a second fact about the same
    subject and predicate **updates** the existing one, and the update path is explicit
    about confidence rather than overwriting silently (``store.upsert_fact``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    robot_id: str
    #: What the fact is about: a person id, ``robot``, a place, an object label.
    subject: str
    #: What is being said about it: ``name``, ``prefers``, ``lives_in``, ``is_called``.
    predicate: str
    value: str
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    provenance: Provenance = Field(default_factory=Provenance)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        """The identity of the fact: one value per (robot, subject, predicate)."""
        return (self.robot_id, self.subject, self.predicate)

    @property
    def text(self) -> str:
        return f"{self.subject} {self.predicate} {self.value}"


class PersonRecord(BaseModel):
    """Somebody the robot knows, and how well.

    ``familiarity`` is derived rather than stored opinion: it rises with interactions and
    decays with absence (:meth:`familiarity_now`), so a person the robot has not seen for
    a month is remembered but no longer treated as a regular.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str
    robot_id: str
    display_name: str = ""
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    interaction_count: int = 0
    #: 0..1, stored. The raw score; :meth:`familiarity_now` applies the decay.
    familiarity: float = Field(default=0.0, ge=0.0, le=1.0)
    #: A reference into the face registry — never an embedding itself.
    embedding_ref: str | None = None
    #: Small, stable things worth keeping on the person rather than as separate facts.
    facts: dict[str, Any] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.display_name or self.person_id

    def days_since_seen(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.last_seen).total_seconds() / 86_400

    def familiarity_now(self, now: datetime | None = None, *, half_life_days: float = 30.0) -> float:
        """Familiarity after absence decay. What ranking and greeting actually read."""
        if half_life_days <= 0:
            return self.familiarity
        factor: float = 0.5 ** (self.days_since_seen(now) / half_life_days)
        return max(0.0, min(1.0, self.familiarity * factor))

    def met(self, *, now: datetime | None = None, weight: float = 0.08) -> PersonRecord:
        """Another interaction: count up, last seen now, familiarity a little higher.

        Familiarity approaches 1 asymptotically rather than incrementing: the difference
        between the first and second meeting matters far more than between the fiftieth
        and the fifty-first.
        """
        moment = now or utcnow()
        return self.model_copy(
            update={
                "last_seen": moment,
                "interaction_count": self.interaction_count + 1,
                "familiarity": min(1.0, self.familiarity + (1.0 - self.familiarity) * weight),
            }
        )


class MemoryQuery(BaseModel):
    """What a caller is asking the memory for. One shape for every store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot_id: str
    text: str = ""
    person_id: str | None = None
    event_types: tuple[EventType, ...] = ()
    since: datetime | None = None
    limit: int = Field(default=20, ge=1, le=500)
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)


class ScoredMemory(BaseModel):
    """One retrieved item with the score that got it there, and why.

    The ``reasons`` are the difference between a retrieval system somebody can tune and
    one they have to trust: a memory that shows up in a prompt can be asked why it did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MemoryKind
    id: str
    text: str
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    person_id: str | None = None
    timestamp: datetime | None = None

    @property
    def approx_tokens(self) -> int:
        return approx_tokens(self.text)


def approx_tokens(text: str) -> int:
    """A cheap token estimate: four characters per token, minimum one.

    Deliberately not a tokenizer. The budget this feeds exists to stop a memory store
    filling a prompt, and for that a consistent over-estimate is worth more than an exact
    count that costs a dependency and a model download.
    """
    return max(1, (len(text) + 3) // 4)


__all__ = [
    "IMPORTANCE_CRITICAL",
    "IMPORTANCE_NORMAL",
    "IMPORTANCE_NOTABLE",
    "IMPORTANCE_TRIVIAL",
    "EpisodicMemory",
    "EventType",
    "MemoryKind",
    "MemoryQuery",
    "PersonRecord",
    "Provenance",
    "ScoredMemory",
    "SemanticFact",
    "approx_tokens",
]
