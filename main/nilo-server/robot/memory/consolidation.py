"""Turning things that happened into things the robot knows.

Consolidation reads unconsolidated episodes and writes semantic facts. It runs on a timer,
off the hot path, and it is **deterministic**: the built-in rules are pattern matches over
event types and metadata, not a language model.

The design rule, stated plainly: **the LLM does not get to overwrite facts.** A summarizer
can propose — :class:`Consolidator` accepts an optional ``summarizer`` — but everything it
proposes goes through the same merge as any other claim
(:func:`~robot.memory.store.merge_fact`): a value that disagrees with a stored one only
wins if it arrives with *higher* confidence, and a losing claim is recorded as disputed
rather than discarded silently. A model having an opinion is not the same as the robot
learning something.

Provenance is written on every fact: what produced it, which episodes it came from, when
it was created, when it was last touched, and how many times it has been confirmed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from robot.memory.models import (
    IMPORTANCE_NOTABLE,
    EpisodicMemory,
    EventType,
    Provenance,
    SemanticFact,
)
from robot.memory.store import MemoryStore
from robot.state.models import utcnow

logger = logging.getLogger(__name__)

#: How often the background job runs when it owns the timer. Consolidation is not urgent —
#: it is the robot thinking about its day, and doing it every few minutes is plenty.
DEFAULT_INTERVAL_S = 300.0

#: How many episodes one pass looks at. Bounded so a robot that has been running for a
#: month does not do a month of work in one pass after a restart.
DEFAULT_BATCH = 100

#: Confidence for a fact derived by a deterministic rule. Below the confidence an operator
#: or the user's own statement carries (1.0 and 0.9), above a model's guess.
RULE_CONFIDENCE = 0.75

#: How many episodes of the same kind it takes before "this keeps happening" is a fact.
DEFAULT_PATTERN_THRESHOLD = 3


@dataclass(frozen=True)
class ConsolidationResult:
    """What one pass did. Returned so a caller (or a test) can assert on it."""

    episodes_read: int = 0
    facts_written: int = 0
    facts: tuple[SemanticFact, ...] = ()
    marked: int = 0

    @property
    def did_something(self) -> bool:
        return bool(self.facts_written or self.marked)


@dataclass
class ConsolidationRule:
    """One deterministic way of turning episodes into a fact.

    ``match`` picks the episodes it cares about; ``derive`` turns the matched set into
    zero or more ``(subject, predicate, value)`` triples. Both are plain functions, so a
    deployment adds a rule without touching the consolidator.
    """

    name: str
    match: Callable[[EpisodicMemory], bool]
    derive: Callable[[Sequence[EpisodicMemory]], list[tuple[str, str, str]]]
    #: How many matching episodes are needed before the rule fires.
    threshold: int = 1
    confidence: float = RULE_CONFIDENCE


def _count_by_person(episodes: Sequence[EpisodicMemory]) -> dict[str, list[EpisodicMemory]]:
    grouped: dict[str, list[EpisodicMemory]] = {}
    for episode in episodes:
        if episode.person_id:
            grouped.setdefault(episode.person_id, []).append(episode)
    return grouped


def _familiar_people(episodes: Sequence[EpisodicMemory]) -> list[tuple[str, str, str]]:
    """People the robot has met enough times to call familiar."""
    facts: list[tuple[str, str, str]] = []
    for person_id, seen in sorted(_count_by_person(episodes).items()):
        if len(seen) >= DEFAULT_PATTERN_THRESHOLD:
            facts.append((person_id, "familiarity", "familiar"))
        named = next((e.metadata.get("name") for e in seen if e.metadata.get("name")), None)
        if named:
            facts.append((person_id, "name", str(named)))
    return facts


def _liked_activities(episodes: Sequence[EpisodicMemory]) -> list[tuple[str, str, str]]:
    """What a person seems to enjoy, from play and praise episodes with a subject."""
    facts: list[tuple[str, str, str]] = []
    for person_id, seen in sorted(_count_by_person(episodes).items()):
        subjects = [str(e.metadata["about"]) for e in seen if e.metadata.get("about")]
        if len(subjects) >= DEFAULT_PATTERN_THRESHOLD:
            most_common = max(sorted(set(subjects)), key=subjects.count)
            facts.append((person_id, "enjoys", most_common))
    return facts


def _recurring_failures(episodes: Sequence[EpisodicMemory]) -> list[tuple[str, str, str]]:
    """Something that keeps going wrong is worth knowing, not just worth logging."""
    subjects = [str(e.metadata.get("about", "")) for e in episodes if e.metadata.get("about")]
    facts: list[tuple[str, str, str]] = []
    for subject in sorted(set(subjects)):
        if subjects.count(subject) >= DEFAULT_PATTERN_THRESHOLD:
            facts.append((subject, "often_fails", "true"))
    return facts


def _known_places(episodes: Sequence[EpisodicMemory]) -> list[tuple[str, str, str]]:
    """Places the robot has reached more than once are places it knows."""
    places = [str(e.metadata.get("place", "")) for e in episodes if e.metadata.get("place")]
    return [
        (place, "is_a", "known_place")
        for place in sorted(set(places))
        if places.count(place) >= 2
    ]


#: The rules that ship. Each is a pattern somebody could explain to the robot's owner.
DEFAULT_RULES: tuple[ConsolidationRule, ...] = (
    ConsolidationRule(
        name="familiar_people",
        match=lambda e: e.event_type in {EventType.MET_PERSON, EventType.CONVERSATION},
        derive=_familiar_people,
    ),
    ConsolidationRule(
        name="liked_activities",
        match=lambda e: e.event_type in {EventType.PLAY, EventType.PRAISE},
        derive=_liked_activities,
    ),
    ConsolidationRule(
        name="recurring_failures",
        match=lambda e: e.event_type is EventType.FAILURE,
        derive=_recurring_failures,
        confidence=0.65,
    ),
    ConsolidationRule(
        name="known_places",
        match=lambda e: e.event_type is EventType.NAVIGATION,
        derive=_known_places,
        confidence=0.6,
    ),
)

#: A summarizer takes the episodes of one pass and proposes facts. Optional, and its
#: output is merged under exactly the same rules as everything else.
Summarizer = Callable[[Sequence[EpisodicMemory]], Awaitable[Sequence[SemanticFact]]]


@dataclass
class Consolidator:
    """The background job. One pass per call, or a timer over it.

    Idempotent: an episode that has been folded in is marked, and a second pass over the
    same data writes nothing new. That is what makes it safe to run on a timer, safe to
    call by hand, and safe to restart.
    """

    store: MemoryStore
    rules: tuple[ConsolidationRule, ...] = DEFAULT_RULES
    batch: int = DEFAULT_BATCH
    #: Optional proposer (an LLM, usually). Its facts are merged, never applied.
    summarizer: Summarizer | None = None
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def run_once(self, robot_id: str, *, now: datetime | None = None) -> ConsolidationResult:
        """Read what has not been consolidated, write what it implies, mark it done."""
        moment = now or utcnow()
        episodes = await self.store.recent_episodes(
            robot_id, limit=self.batch, unconsolidated_only=True
        )
        if not episodes:
            return ConsolidationResult()

        written: list[SemanticFact] = []
        for rule in self.rules:
            matched = [episode for episode in episodes if rule.match(episode)]
            if len(matched) < rule.threshold:
                continue
            source_ids = tuple(episode.id for episode in matched)
            for subject, predicate, value in rule.derive(matched):
                fact = SemanticFact(
                    robot_id=robot_id,
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    confidence=rule.confidence,
                    provenance=Provenance(
                        learned_from=f"consolidation:{rule.name}",
                        source_ids=source_ids,
                        created_at=moment,
                        updated_at=moment,
                    ),
                )
                written.append(await self.store.upsert_fact(fact))

        if self.summarizer is not None:
            written.extend(await self._summarize(robot_id, episodes, moment))

        marked = await self.store.mark_consolidated([episode.id for episode in episodes], now=moment)
        logger.info(
            "robot %s: consolidated %d episode(s) into %d fact(s)", robot_id, len(episodes), len(written)
        )
        return ConsolidationResult(
            episodes_read=len(episodes),
            facts_written=len(written),
            facts=tuple(written),
            marked=marked,
        )

    async def _summarize(
        self, robot_id: str, episodes: Sequence[EpisodicMemory], now: datetime
    ) -> list[SemanticFact]:
        """Let a proposer suggest facts, and merge them like anybody else's.

        Deliberately defensive: a summarizer that raises, hangs or returns nonsense must
        not stop the deterministic half from having run.
        """
        assert self.summarizer is not None
        try:
            proposed = await self.summarizer(episodes)
        except Exception as exc:
            logger.warning("robot %s: the memory summarizer failed (%s); rules still applied", robot_id, exc)
            return []
        written: list[SemanticFact] = []
        for fact in proposed:
            if fact.robot_id != robot_id or not fact.subject or not fact.predicate:
                logger.warning("robot %s: dropping a malformed proposed fact", robot_id)
                continue
            # The proposal keeps its own confidence, and merge_fact decides whether that is
            # enough to change anything. This is the only door a model has into memory.
            provenance = fact.provenance.model_copy(
                update={
                    "learned_from": fact.provenance.learned_from or "summarizer",
                    "source_ids": tuple(episode.id for episode in episodes),
                    "updated_at": now,
                }
            )
            written.append(await self.store.upsert_fact(fact.model_copy(update={"provenance": provenance})))
        return written

    async def run(self, robot_id: str, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        """Run forever on a timer. One pass at a time; a slow pass delays, never overlaps."""
        while not self._closed:
            await asyncio.sleep(interval_s)
            try:
                await self.run_once(robot_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("robot %s: consolidation pass failed", robot_id)

    def start(self, robot_id: str, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self.run(robot_id, interval_s=interval_s), name=f"robot-memory-consolidation-{robot_id}"
        )

    async def aclose(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def important(episode: EpisodicMemory) -> bool:
    """Whether an episode is worth keeping when memory is being trimmed."""
    return episode.importance >= IMPORTANCE_NOTABLE


__all__ = [
    "DEFAULT_BATCH",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_PATTERN_THRESHOLD",
    "DEFAULT_RULES",
    "RULE_CONFIDENCE",
    "ConsolidationResult",
    "ConsolidationRule",
    "Consolidator",
    "Summarizer",
    "important",
]
