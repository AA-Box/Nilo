"""Choosing what the robot should remember *right now*, inside a budget.

The rule this module exists for: **the memory database never goes into the prompt.** A
retrieval pass returns a bounded, ranked, explainable selection — and the bound is in
tokens, not in rows, because a row can be a sentence or a paragraph.

Ranking combines four signals, each of which earns its place:

``relevance``   lexical overlap with the query, plus embedding similarity when enabled
``recency``     exponential decay on age. Yesterday beats last month
``importance``  what the writer said the memory was worth, 0..1
``association`` a large, flat bonus for the person the robot is talking to

No signal is allowed to be the only one. Pure similarity misses "what happened just now";
pure recency misses "the thing she told me last week"; pure importance returns the same
five memories forever.

Every returned item carries the reasons it was chosen, so a prompt that contains a strange
memory can be asked why.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from robot.memory.embeddings import EmbeddingProvider, NullEmbeddingProvider, cosine
from robot.memory.models import (
    EpisodicMemory,
    MemoryKind,
    MemoryQuery,
    PersonRecord,
    ScoredMemory,
    SemanticFact,
    approx_tokens,
)
from robot.memory.store import MemoryStore
from robot.state.models import utcnow

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class RetrievalWeights:
    """What each signal is worth. Every number the ranking uses lives here.

    Tuned so that: a directly relevant memory beats a merely recent one; a memory about
    the person in front of the robot beats an equally relevant one about a stranger; and
    an important memory stays reachable long after it stops being recent.
    """

    relevance: float = 0.45
    recency: float = 0.25
    importance: float = 0.20
    association: float = 0.10
    #: Age at which the recency signal has halved, in seconds. A day, so "this morning"
    #: and "last week" are meaningfully different and "last month" is nearly flat.
    recency_half_life_s: float = 86_400.0
    #: Facts are stable, so their recency signal is replaced by their confidence.
    fact_confidence_weight: float = 0.35


@dataclass(frozen=True)
class RetrievalBudget:
    """How much a caller is willing to spend on remembered context.

    ``max_tokens`` is the real limit; ``max_items`` stops a pathological set of one-word
    memories filling the list. Both are ceilings, and retrieval returns less than either
    whenever there is less worth saying.
    """

    max_tokens: int = 400
    max_items: int = 8
    #: Below this score a memory is not worth its tokens, whatever the budget allows.
    min_score: float = 0.08


class MemoryRetriever:
    """Ranks and trims what the stores hold, for one query.

    Constructed with a store and (optionally) an embedding provider. With the default
    :class:`~robot.memory.embeddings.NullEmbeddingProvider` every path here still works —
    that is the point, and a test asserts it.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        embeddings: EmbeddingProvider | None = None,
        weights: RetrievalWeights | None = None,
        budget: RetrievalBudget | None = None,
    ) -> None:
        self.store = store
        self.embeddings: EmbeddingProvider = NullEmbeddingProvider() if embeddings is None else embeddings
        self.weights = weights or RetrievalWeights()
        self.budget = budget or RetrievalBudget()

    async def retrieve(
        self,
        query: MemoryQuery,
        *,
        budget: RetrievalBudget | None = None,
        now: datetime | None = None,
    ) -> list[ScoredMemory]:
        """The bounded, ranked selection for one query. Never the whole database."""
        moment = now or utcnow()
        limit = budget or self.budget
        query_vector = await self._embed(query.text)
        candidates: list[ScoredMemory] = []

        episodes = await self.store.recent_episodes(
            query.robot_id,
            limit=max(query.limit, limit.max_items * 4),
            person_id=query.person_id if query.person_id and query.event_types else None,
            event_types=query.event_types,
            since=query.since,
            min_importance=query.min_importance,
        )
        for episode in episodes:
            candidates.append(await self._score_episode(episode, query, query_vector, moment))

        for fact in await self.store.facts(query.robot_id, limit=limit.max_items * 4):
            candidates.append(self._score_fact(fact, query))

        if query.person_id:
            person = await self.store.get_person(query.robot_id, query.person_id)
            if person is not None:
                candidates.append(self._score_person(person, moment))

        candidates.sort(key=lambda item: (-item.score, item.id))
        return self._within_budget(candidates, limit)

    async def context(
        self,
        query: MemoryQuery,
        *,
        budget: RetrievalBudget | None = None,
        now: datetime | None = None,
    ) -> str:
        """The same selection as text, ready to drop into a prompt.

        One line per memory, most relevant first, with nothing else added: this is context
        for a model, not a report for a person.
        """
        selected = await self.retrieve(query, budget=budget, now=now)
        return "\n".join(f"- {item.text}" for item in selected)

    # -- scoring ------------------------------------------------------------------------------

    async def _score_episode(
        self,
        episode: EpisodicMemory,
        query: MemoryQuery,
        query_vector: Sequence[float],
        now: datetime,
    ) -> ScoredMemory:
        weights = self.weights
        reasons: list[str] = []

        relevance = lexical_overlap(query.text, episode.text)
        if query_vector:
            stored = episode.metadata.get("embedding")
            if isinstance(stored, list) and stored:
                similarity = cosine(query_vector, [float(value) for value in stored])
                relevance = max(relevance, similarity)
                if similarity > 0.6:
                    reasons.append(f"semantically similar ({similarity:.2f})")
        if relevance > 0.2 and not reasons:
            reasons.append(f"mentions the query ({relevance:.2f})")

        age = episode.age_s(now)
        recency = 0.5 ** (age / weights.recency_half_life_s)
        if recency > 0.5:
            reasons.append("recent")
        if episode.importance >= 0.7:
            reasons.append(f"marked important ({episode.importance:.2f})")

        association = 0.0
        if query.person_id and episode.person_id == query.person_id:
            association = 1.0
            reasons.append(f"about {query.person_id}")

        score = (
            weights.relevance * relevance
            + weights.recency * recency
            + weights.importance * episode.importance
            + weights.association * association
        )
        return ScoredMemory(
            kind=MemoryKind.EPISODIC,
            id=episode.id,
            text=episode.text,
            score=round(score, 4),
            reasons=tuple(reasons),
            person_id=episode.person_id,
            timestamp=episode.timestamp,
        )

    def _score_fact(self, fact: SemanticFact, query: MemoryQuery) -> ScoredMemory:
        weights = self.weights
        reasons: list[str] = ["a stable fact"]
        relevance = lexical_overlap(query.text, fact.text)
        association = 0.0
        if query.person_id and fact.subject == query.person_id:
            association = 1.0
            reasons.append(f"about {query.person_id}")
        if relevance > 0.2:
            reasons.append(f"mentions the query ({relevance:.2f})")
        score = (
            weights.relevance * relevance
            + weights.fact_confidence_weight * fact.confidence
            + weights.association * association
        )
        return ScoredMemory(
            kind=MemoryKind.SEMANTIC,
            id=fact.id,
            text=fact.text,
            score=round(score, 4),
            reasons=tuple(reasons),
            person_id=fact.subject if fact.subject.startswith("person") else None,
            timestamp=fact.provenance.updated_at,
        )

    def _score_person(self, person: PersonRecord, now: datetime) -> ScoredMemory:
        familiarity = person.familiarity_now(now)
        text = (
            f"{person.name}: known for {person.interaction_count} interactions, "
            f"familiarity {familiarity:.2f}, last seen {person.days_since_seen(now):.1f} days ago"
        )
        return ScoredMemory(
            kind=MemoryKind.PERSON,
            id=person.person_id,
            text=text,
            score=round(0.5 + 0.5 * familiarity, 4),
            reasons=("the person being spoken to",),
            person_id=person.person_id,
            timestamp=person.last_seen,
        )

    def _within_budget(self, candidates: Sequence[ScoredMemory], budget: RetrievalBudget) -> list[ScoredMemory]:
        """Take from the top until the token budget or the item ceiling is reached."""
        selected: list[ScoredMemory] = []
        spent = 0
        for candidate in candidates:
            if len(selected) >= budget.max_items:
                break
            if candidate.score < budget.min_score:
                break  # sorted, so everything after this is worse too
            cost = candidate.approx_tokens
            if spent + cost > budget.max_tokens:
                continue  # a smaller one further down may still fit
            selected.append(candidate)
            spent += cost
        return selected

    async def _embed(self, text: str) -> list[float]:
        if not text or not self.embeddings.enabled:
            return []
        try:
            return await self.embeddings.embed(text)
        except Exception as exc:  # an embedding provider that fails is not a retrieval failure
            logger.warning("memory: embedding failed (%s); falling back to lexical ranking", exc)
            return []


def lexical_overlap(query: str, text: str) -> float:
    """Fraction of the query's words that appear in the text. The no-dependency relevance.

    Crude, and deliberately so: it costs nothing, needs no model, is identical on every
    machine, and is the floor every other signal is added on top of.
    """
    query_words = set(_WORD.findall(query.lower()))
    if not query_words:
        return 0.0
    text_words = set(_WORD.findall(text.lower()))
    if not text_words:
        return 0.0
    return len(query_words & text_words) / len(query_words)


def total_tokens(items: Sequence[ScoredMemory]) -> int:
    return sum(approx_tokens(item.text) for item in items)


def _half_life(age_s: float, half_life_s: float) -> float:  # pragma: no cover - kept for clarity
    return math.pow(0.5, age_s / half_life_s) if half_life_s > 0 else 0.0


__all__ = [
    "MemoryRetriever",
    "RetrievalBudget",
    "RetrievalWeights",
    "lexical_overlap",
    "total_tokens",
]
