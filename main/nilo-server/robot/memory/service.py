"""One object that owns a robot's memory, and the privacy operations on it.

    memory = RobotMemory("nilo-sim-01", store)
    await memory.open()

    await memory.remember("met Ahmad", event_type=EventType.MET_PERSON, person_id="ahmad")
    context = await memory.context_for("what do we usually play with?", person_id="ahmad")

    await memory.forget_person("ahmad")     # person, episodes and facts about them
    await memory.clear()                    # everything this robot remembers

The four stores are here together because callers do not want four objects: recording that
the robot met somebody should update the person record *and* write an episode, and that is
one call, not two that can be forgotten separately.

The privacy operations are first-class, not an afterthought: list, delete one, delete a
person and everything about them, clear a robot. A memory system without a delete is a
liability, and one whose delete leaves the episodes behind is worse, because it looks like
it worked.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from robot.memory.consolidation import Consolidator
from robot.memory.embeddings import EmbeddingProvider, NullEmbeddingProvider
from robot.memory.models import (
    IMPORTANCE_NORMAL,
    EpisodicMemory,
    EventType,
    MemoryKind,
    MemoryQuery,
    PersonRecord,
    Provenance,
    ScoredMemory,
    SemanticFact,
)
from robot.memory.retrieval import MemoryRetriever, RetrievalBudget
from robot.memory.store import MemoryStore
from robot.memory.working import WorkingMemory
from robot.state.models import utcnow

logger = logging.getLogger(__name__)


class RobotMemory:
    """Working, episodic, semantic and person memory for one robot."""

    def __init__(
        self,
        robot_id: str,
        store: MemoryStore,
        *,
        embeddings: EmbeddingProvider | None = None,
        budget: RetrievalBudget | None = None,
        working: WorkingMemory | None = None,
        consolidator: Consolidator | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.store = store
        self.embeddings: EmbeddingProvider = NullEmbeddingProvider() if embeddings is None else embeddings
        self.working = WorkingMemory(robot_id) if working is None else working
        self.retriever = MemoryRetriever(store, embeddings=self.embeddings, budget=budget)
        self.consolidator = Consolidator(store) if consolidator is None else consolidator

    async def open(self) -> None:
        await self.store.open()

    async def aclose(self) -> None:
        await self.consolidator.aclose()

    # -- writing -------------------------------------------------------------------------------

    async def remember(
        self,
        summary: str,
        *,
        event_type: EventType = EventType.OBSERVATION,
        person_id: str | None = None,
        importance: float = IMPORTANCE_NORMAL,
        timestamp: datetime | None = None,
        **metadata: Any,
    ) -> EpisodicMemory:
        """Write one episode. The main way anything gets into long-term memory.

        When embeddings are enabled the text is embedded here, once, on the way in — never
        at query time, where it would be a model call per retrieval.
        """
        episode = EpisodicMemory(
            robot_id=self.robot_id,
            event_type=event_type,
            summary=summary,
            person_id=person_id,
            importance=importance,
            timestamp=timestamp or utcnow(),
            metadata=dict(metadata),
        )
        if self.embeddings.enabled:
            try:
                vector = await self.embeddings.embed(episode.text)
                if vector:
                    episode = episode.model_copy(
                        update={"metadata": {**episode.metadata, "embedding": vector}}
                    )
            except Exception as exc:  # an embedding failure must not lose the memory
                logger.warning("robot %s: could not embed a memory (%s); storing it anyway", self.robot_id, exc)
        return await self.store.add_episode(episode)

    async def met_person(
        self,
        person_id: str,
        *,
        display_name: str = "",
        embedding_ref: str | None = None,
        summary: str = "",
        now: datetime | None = None,
    ) -> PersonRecord:
        """Record an interaction with somebody: the person record **and** the episode."""
        moment = now or utcnow()
        existing = await self.store.get_person(self.robot_id, person_id)
        if existing is None:
            record = PersonRecord(
                person_id=person_id,
                robot_id=self.robot_id,
                display_name=display_name,
                first_seen=moment,
                last_seen=moment,
                embedding_ref=embedding_ref,
            ).met(now=moment)
        else:
            record = existing.met(now=moment)
            if display_name:
                record = record.model_copy(update={"display_name": display_name})
            if embedding_ref:
                record = record.model_copy(update={"embedding_ref": embedding_ref})
        stored = await self.store.upsert_person(record)
        await self.remember(
            summary or f"met {stored.name}",
            event_type=EventType.MET_PERSON,
            person_id=person_id,
            timestamp=moment,
            name=stored.display_name or None,
        )
        return stored

    async def learn(
        self,
        subject: str,
        predicate: str,
        value: str,
        *,
        confidence: float = 0.8,
        learned_from: str = "user",
        source_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> SemanticFact:
        """Write a semantic fact, with provenance, through the merge rules."""
        moment = now or utcnow()
        return await self.store.upsert_fact(
            SemanticFact(
                robot_id=self.robot_id,
                subject=subject,
                predicate=predicate,
                value=value,
                confidence=confidence,
                provenance=Provenance(
                    learned_from=learned_from,
                    source_ids=tuple(source_ids),
                    created_at=moment,
                    updated_at=moment,
                ),
            )
        )

    # -- reading -------------------------------------------------------------------------------

    async def recall(
        self,
        text: str = "",
        *,
        person_id: str | None = None,
        limit: int = 20,
        budget: RetrievalBudget | None = None,
        now: datetime | None = None,
    ) -> list[ScoredMemory]:
        """The bounded, ranked, explained selection for one question."""
        query = MemoryQuery(robot_id=self.robot_id, text=text, person_id=person_id, limit=limit)
        return await self.retriever.retrieve(query, budget=budget, now=now)

    async def context_for(
        self,
        text: str = "",
        *,
        person_id: str | None = None,
        budget: RetrievalBudget | None = None,
        include_working: bool = True,
        now: datetime | None = None,
    ) -> str:
        """What to put in front of an LLM before it answers. Bounded, and it says so.

        Long-term memory first, then the current exchange — a model reads the top of its
        context best, and the conversation is already in the message history anyway.
        """
        remembered = await self.retriever.context(
            MemoryQuery(robot_id=self.robot_id, text=text, person_id=person_id),
            budget=budget,
            now=now,
        )
        if not include_working:
            return remembered
        transcript = self.working.transcript()
        if not transcript:
            return remembered
        if not remembered:
            return transcript
        return f"{remembered}\n\nCurrent exchange:\n{transcript}"

    async def person(self, person_id: str) -> PersonRecord | None:
        return await self.store.get_person(self.robot_id, person_id)

    async def people(self, limit: int = 100) -> list[PersonRecord]:
        return await self.store.people(self.robot_id, limit=limit)

    async def episodes(self, **filters: Any) -> list[EpisodicMemory]:
        return await self.store.recent_episodes(self.robot_id, **filters)

    async def facts(self, *, subject: str | None = None, limit: int = 100) -> list[SemanticFact]:
        return await self.store.facts(self.robot_id, subject=subject, limit=limit)

    async def counts(self) -> dict[str, int]:
        counts = await self.store.counts(self.robot_id)
        counts["working"] = len(self.working)
        return counts

    # -- consolidation --------------------------------------------------------------------------

    async def consolidate(self, *, now: datetime | None = None) -> Any:
        return await self.consolidator.run_once(self.robot_id, now=now)

    def start_consolidation(self, *, interval_s: float | None = None) -> None:
        if interval_s is None:
            self.consolidator.start(self.robot_id)
        else:
            self.consolidator.start(self.robot_id, interval_s=interval_s)

    # -- privacy ---------------------------------------------------------------------------------

    async def forget(self, kind: MemoryKind, memory_id: str) -> bool:
        """Delete one memory by kind and id."""
        if kind is MemoryKind.EPISODIC:
            return await self.store.delete_episode(memory_id)
        if kind is MemoryKind.SEMANTIC:
            return await self.store.delete_fact(memory_id)
        if kind is MemoryKind.PERSON:
            return await self.store.delete_person(self.robot_id, memory_id) > 0
        self.working.clear()
        return True

    async def forget_person(self, person_id: str) -> int:
        """Delete a person, their episodes and every fact about them. Returns rows removed."""
        removed = await self.store.delete_person(self.robot_id, person_id)
        logger.info("robot %s: forgot person %s (%d rows)", self.robot_id, person_id, removed)
        return removed

    async def clear(self) -> int:
        """Forget everything this robot remembers, including the current exchange."""
        self.working.clear()
        removed = await self.store.clear_robot(self.robot_id)
        logger.info("robot %s: memory cleared (%d rows)", self.robot_id, removed)
        return removed

    def __repr__(self) -> str:
        return f"<RobotMemory {self.robot_id} embeddings={self.embeddings.enabled}>"


__all__ = ["RobotMemory"]
