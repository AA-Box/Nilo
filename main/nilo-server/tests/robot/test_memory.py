"""Robot memory: persistence, retrieval, ranking, consolidation, deletion, restart.

Every test runs against a real SQLite database — in memory for the fast ones, on disk for
the two that are about surviving a restart. No mocked store: the point of this layer is
that it writes things down, and a fake store cannot fail to.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from robot.memory.consolidation import DEFAULT_PATTERN_THRESHOLD, Consolidator
from robot.memory.embeddings import (
    HashingEmbeddingProvider,
    NullEmbeddingProvider,
    cosine,
)
from robot.memory.models import (
    IMPORTANCE_CRITICAL,
    IMPORTANCE_TRIVIAL,
    EpisodicMemory,
    EventType,
    MemoryKind,
    MemoryQuery,
    PersonRecord,
    Provenance,
    SemanticFact,
    approx_tokens,
)
from robot.memory.retrieval import MemoryRetriever, RetrievalBudget, lexical_overlap
from robot.memory.service import RobotMemory
from robot.memory.store import IN_MEMORY, MIGRATIONS, SqliteMemoryStore, merge_fact
from robot.memory.working import WorkingMemory
from robot.state.models import utcnow
from tests.robot.conftest import ROBOT_ID, FakeClock

T0 = utcnow()


def at(seconds: float):
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
async def store():
    """A fresh in-memory database per test, migrated."""
    active = SqliteMemoryStore(IN_MEMORY)
    await active.open()
    try:
        yield active
    finally:
        await active.aclose()


@pytest.fixture
async def memory(store):
    active = RobotMemory(ROBOT_ID, store)
    try:
        yield active
    finally:
        await active.aclose()


def episode(summary: str, **kwargs) -> EpisodicMemory:
    kwargs.setdefault("robot_id", ROBOT_ID)
    return EpisodicMemory(summary=summary, **kwargs)


# -- schema and migrations --------------------------------------------------------------------


async def test_migrations_run_once_and_are_idempotent(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    first = SqliteMemoryStore(path)
    await first.open()
    assert await first.schema_version() == MIGRATIONS[-1][0]
    await first.aclose()

    second = SqliteMemoryStore(path)
    await second.open()
    assert await second.schema_version() == MIGRATIONS[-1][0]
    await second.aclose()


async def test_the_store_refuses_to_be_used_before_it_is_opened():
    closed = SqliteMemoryStore(IN_MEMORY)
    with pytest.raises(RuntimeError, match="not open"):
        await closed.recent_episodes(ROBOT_ID)


# -- episodic --------------------------------------------------------------------------------------


async def test_an_episode_round_trips_with_every_field(store):
    written = await store.add_episode(
        episode(
            "met Ahmad",
            event_type=EventType.MET_PERSON,
            person_id="ahmad",
            importance=IMPORTANCE_CRITICAL,
            timestamp=T0,
            metadata={"place": "kitchen"},
        )
    )
    read = await store.get_episode(written.id)
    assert read is not None
    assert (read.summary, read.person_id, read.event_type) == ("met Ahmad", "ahmad", EventType.MET_PERSON)
    assert read.importance == IMPORTANCE_CRITICAL
    assert read.metadata == {"place": "kitchen"}
    assert read.timestamp == T0
    assert read.consolidated_at is None


async def test_episodes_come_back_newest_first_and_can_be_filtered(store):
    await store.add_episode(episode("old", timestamp=at(-3600)))
    await store.add_episode(episode("new", timestamp=at(-10)))
    await store.add_episode(episode("about ahmad", person_id="ahmad", timestamp=at(-20)))
    await store.add_episode(
        episode("a failure", event_type=EventType.FAILURE, timestamp=at(-30), importance=0.9)
    )

    assert [e.summary for e in await store.recent_episodes(ROBOT_ID)][0] == "new"
    assert [e.summary for e in await store.recent_episodes(ROBOT_ID, person_id="ahmad")] == ["about ahmad"]
    assert [
        e.summary for e in await store.recent_episodes(ROBOT_ID, event_types=[EventType.FAILURE])
    ] == ["a failure"]
    assert [e.summary for e in await store.recent_episodes(ROBOT_ID, min_importance=0.8)] == ["a failure"]
    assert [e.summary for e in await store.recent_episodes(ROBOT_ID, since=at(-60))] != ["old"]
    assert len(await store.recent_episodes(ROBOT_ID, limit=2)) == 2


async def test_memories_are_scoped_to_one_robot(store):
    await store.add_episode(episode("mine"))
    await store.add_episode(EpisodicMemory(robot_id="other-robot", summary="theirs"))
    assert [e.summary for e in await store.recent_episodes(ROBOT_ID)] == ["mine"]
    assert [e.summary for e in await store.recent_episodes("other-robot")] == ["theirs"]


async def test_an_episode_needs_a_robot():
    with pytest.raises(Exception, match="must belong to a robot"):
        EpisodicMemory(robot_id="  ", summary="orphan")


# -- semantic and provenance ---------------------------------------------------------------------------


def fact(value: str, *, confidence: float = 0.7, learned_from: str = "user") -> SemanticFact:
    return SemanticFact(
        robot_id=ROBOT_ID,
        subject="ahmad",
        predicate="favourite_colour",
        value=value,
        confidence=confidence,
        provenance=Provenance(learned_from=learned_from, created_at=T0, updated_at=T0),
    )


async def test_a_fact_carries_its_provenance(store):
    written = await store.upsert_fact(fact("blue"))
    read = await store.get_fact(ROBOT_ID, "ahmad", "favourite_colour")
    assert read is not None
    assert read.value == "blue"
    assert read.provenance.learned_from == "user"
    assert read.provenance.created_at == T0
    assert read.id == written.id


async def test_the_same_value_again_is_a_confirmation_not_a_rewrite(store):
    first = await store.upsert_fact(fact("blue", confidence=0.6))
    second = await store.upsert_fact(fact("blue", confidence=0.6))
    assert second.id == first.id
    assert second.confidence > first.confidence
    assert second.provenance.confirmations == 1
    assert second.provenance.created_at == first.provenance.created_at


async def test_a_more_confident_claim_wins_and_keeps_the_history(store):
    await store.upsert_fact(fact("blue", confidence=0.5))
    updated = await store.upsert_fact(fact("green", confidence=0.9, learned_from="operator"))
    assert updated.value == "green"
    assert updated.metadata["previous_value"] == "blue"
    assert updated.provenance.created_at == T0  # the original creation time survives


async def test_a_less_confident_claim_does_not_change_the_answer(store):
    """The rule: a model having an opinion is not the robot learning something."""
    await store.upsert_fact(fact("blue", confidence=0.9, learned_from="user"))
    attempted = await store.upsert_fact(fact("red", confidence=0.4, learned_from="llm"))
    assert attempted.value == "blue"
    assert attempted.metadata["disputed"][-1]["value"] == "red"
    stored = await store.get_fact(ROBOT_ID, "ahmad", "favourite_colour")
    assert stored is not None and stored.value == "blue"


def test_disputes_are_bounded():
    existing = fact("blue", confidence=0.9)
    for index in range(10):
        existing = merge_fact(existing, fact(f"guess-{index}", confidence=0.1))
    assert len(existing.metadata["disputed"]) == 5


# -- people -------------------------------------------------------------------------------------------


async def test_a_person_record_accumulates_interactions(memory):
    first = await memory.met_person("ahmad", display_name="Ahmad")
    assert first.interaction_count == 1
    assert first.familiarity > 0

    second = await memory.met_person("ahmad")
    assert second.interaction_count == 2
    assert second.familiarity > first.familiarity
    assert second.display_name == "Ahmad"  # not lost by the second call


async def test_meeting_somebody_writes_an_episode_too(memory):
    await memory.met_person("ahmad", display_name="Ahmad")
    episodes = await memory.episodes(person_id="ahmad")
    assert [e.event_type for e in episodes] == [EventType.MET_PERSON]


def test_familiarity_decays_with_absence():
    person = PersonRecord(person_id="ahmad", robot_id=ROBOT_ID, familiarity=0.8, last_seen=T0)
    assert person.familiarity_now(T0) == pytest.approx(0.8)
    assert person.familiarity_now(T0 + timedelta(days=30)) == pytest.approx(0.4, abs=0.01)
    assert person.familiarity_now(T0 + timedelta(days=365)) < 0.05


def test_familiarity_approaches_one_without_reaching_it():
    person = PersonRecord(person_id="p", robot_id=ROBOT_ID)
    for _ in range(200):
        person = person.met()
    assert 0.9 < person.familiarity <= 1.0


async def test_a_person_can_carry_a_face_embedding_reference(memory):
    stored = await memory.met_person("ahmad", embedding_ref="abc123")
    assert stored.embedding_ref == "abc123"
    assert (await memory.person("ahmad")).embedding_ref == "abc123"


# -- working memory ---------------------------------------------------------------------------------------


def test_working_memory_is_bounded():
    clock = FakeClock()
    working = WorkingMemory(ROBOT_ID, max_turns=3, clock=clock)
    for index in range(6):
        working.add(f"turn {index}")
    assert len(working) == 3
    assert working.recent()[-1].text == "turn 5"


def test_working_memory_expires():
    clock = FakeClock()
    working = WorkingMemory(ROBOT_ID, ttl_s=60.0, clock=clock)
    working.add("hello")
    clock.advance(30)
    assert len(working) == 1
    clock.advance(40)
    assert len(working) == 0
    assert working.transcript() == ""


def test_working_memory_knows_who_is_talking():
    working = WorkingMemory(ROBOT_ID, clock=FakeClock())
    working.add("hello", person_id="ahmad")
    working.add("hello back", role="robot")
    assert working.current_person == "ahmad"
    assert "ahmad: hello" in working.transcript()


# -- retrieval ----------------------------------------------------------------------------------------------


async def test_retrieval_is_bounded_by_tokens_not_rows(memory):
    for index in range(40):
        await memory.remember(f"a memory about cubes number {index}", timestamp=at(-index))
    budget = RetrievalBudget(max_tokens=60, max_items=50)
    selected = await memory.recall("cubes", budget=budget)
    assert selected
    assert sum(item.approx_tokens for item in selected) <= 60
    assert len(selected) < 40


async def test_retrieval_never_returns_the_whole_database(memory):
    for index in range(100):
        await memory.remember(f"memory {index}")
    assert len(await memory.recall("memory")) <= RetrievalBudget().max_items


async def test_relevance_recency_importance_and_association_all_count(memory):
    await memory.remember("we played with the red cube", timestamp=at(-86_400 * 5), person_id="ahmad")
    await memory.remember("nothing happened", timestamp=at(-5))
    await memory.remember("the charger failed", event_type=EventType.FAILURE, importance=0.95, timestamp=at(-3600))

    by_relevance = await memory.recall("red cube")
    assert "red cube" in by_relevance[0].text

    by_recency = await memory.recall("")
    assert any("nothing happened" in item.text for item in by_recency[:2])

    # Association is a tie-breaker between comparable memories, not an override: a
    # five-day-old memory does not leapfrog a recent critical failure just because it is
    # about the person in the room.
    about_ahmad = await memory.recall("", person_id="ahmad")
    assert any("about ahmad" in reason for item in about_ahmad for reason in item.reasons)


async def test_a_memory_about_the_person_in_the_room_beats_an_equal_one_that_is_not(memory):
    await memory.remember("we sat quietly", timestamp=at(-600), person_id="ahmad")
    await memory.remember("we sat quietly", timestamp=at(-600))
    ranked = await memory.recall("", person_id="ahmad")
    assert ranked[0].person_id == "ahmad"
    assert ranked[0].score > ranked[1].score


async def test_every_returned_memory_explains_why_it_is_there(memory):
    await memory.remember("played with the cube", importance=0.9, person_id="ahmad")
    selected = await memory.recall("cube", person_id="ahmad")
    assert selected
    assert all(item.reasons for item in selected)


async def test_facts_and_people_are_retrieved_alongside_episodes(memory):
    await memory.met_person("ahmad", display_name="Ahmad")
    await memory.learn("ahmad", "enjoys", "cubes", confidence=0.9)
    selected = await memory.recall("cubes", person_id="ahmad")
    kinds = {item.kind for item in selected}
    assert MemoryKind.SEMANTIC in kinds
    assert MemoryKind.PERSON in kinds


async def test_context_is_text_and_includes_the_current_exchange(memory):
    await memory.remember("we played with the cube", person_id="ahmad")
    memory.working.add("what did we do yesterday?", person_id="ahmad")
    context = await memory.context_for("cube", person_id="ahmad")
    assert "played with the cube" in context
    assert "Current exchange:" in context
    assert "what did we do yesterday?" in context


def test_the_token_estimate_is_cheap_and_never_zero():
    assert approx_tokens("") == 1
    assert approx_tokens("a" * 40) == 10


def test_lexical_overlap_needs_no_model():
    assert lexical_overlap("red cube", "we played with the red cube") == pytest.approx(1.0)
    assert lexical_overlap("red cube", "nothing here") == 0.0
    assert lexical_overlap("", "anything") == 0.0


# -- embeddings are optional ------------------------------------------------------------------------------------


async def test_everything_works_with_embeddings_disabled(store):
    memory = RobotMemory(ROBOT_ID, store, embeddings=NullEmbeddingProvider())
    await memory.remember("we played with the red cube", person_id="ahmad")
    selected = await memory.recall("red cube", person_id="ahmad")
    assert selected
    assert "red cube" in selected[0].text
    assert await memory.retriever._embed("anything") == []


async def test_enabling_embeddings_adds_a_signal_without_replacing_the_others(store):
    memory = RobotMemory(ROBOT_ID, store, embeddings=HashingEmbeddingProvider())
    await memory.remember("the robot drove to the kitchen and found the cube")
    stored = (await memory.episodes())[0]
    assert isinstance(stored.metadata.get("embedding"), list)
    assert len(stored.metadata["embedding"]) == HashingEmbeddingProvider().dimensions

    selected = await memory.recall("kitchen cube")
    assert selected


def test_the_hashing_provider_is_deterministic_and_normalized():
    provider = HashingEmbeddingProvider()
    first = provider.embed_sync("the red cube")
    second = provider.embed_sync("the red cube")
    assert first == second
    assert cosine(first, second) == pytest.approx(1.0)
    assert cosine(first, provider.embed_sync("")) == 0.0


async def test_an_embedding_failure_does_not_lose_the_memory(store):
    class Broken:
        name = "broken"
        enabled = True

        async def embed(self, text: str) -> list[float]:
            raise RuntimeError("the model is not loaded")

    memory = RobotMemory(ROBOT_ID, store, embeddings=Broken())
    await memory.remember("something worth keeping")
    assert [e.summary for e in await memory.episodes()] == ["something worth keeping"]
    assert await memory.recall("something") != []


# -- consolidation -----------------------------------------------------------------------------------------------


async def test_repeated_meetings_become_a_fact(memory):
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await memory.met_person("ahmad", display_name="Ahmad")
    result = await memory.consolidate()
    assert result.facts_written >= 1
    facts = {(f.subject, f.predicate, f.value) for f in await memory.facts()}
    assert ("ahmad", "familiarity", "familiar") in facts
    assert ("ahmad", "name", "Ahmad") in facts


async def test_consolidation_records_which_episodes_a_fact_came_from(memory):
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await memory.met_person("ahmad", display_name="Ahmad")
    await memory.consolidate()
    fact_row = next(f for f in await memory.facts() if f.predicate == "familiarity")
    assert fact_row.provenance.learned_from.startswith("consolidation:")
    assert len(fact_row.provenance.source_ids) >= DEFAULT_PATTERN_THRESHOLD


async def test_consolidation_is_idempotent(memory):
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await memory.met_person("ahmad")
    first = await memory.consolidate()
    second = await memory.consolidate()
    assert first.episodes_read > 0
    assert second.episodes_read == 0
    assert second.facts_written == 0


async def test_a_repeated_failure_becomes_something_the_robot_knows(memory):
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await memory.remember(
            "could not reach the charger", event_type=EventType.FAILURE, about="charger"
        )
    await memory.consolidate()
    assert ("charger", "often_fails", "true") in {
        (f.subject, f.predicate, f.value) for f in await memory.facts()
    }


async def test_a_summarizer_may_propose_but_never_overwrite(store):
    """The hard rule of this phase, as a test."""
    memory = RobotMemory(ROBOT_ID, store)
    await memory.learn("robot", "name", "Nilo", confidence=0.95, learned_from="operator")

    async def confident_nonsense(episodes):
        return [
            SemanticFact(
                robot_id=ROBOT_ID,
                subject="robot",
                predicate="name",
                value="Ziggy",
                confidence=0.4,
                provenance=Provenance(learned_from="llm"),
            )
        ]

    memory.consolidator = Consolidator(store, summarizer=confident_nonsense)
    await memory.remember("chatted about names", event_type=EventType.CONVERSATION)
    await memory.consolidate()

    stored = await store.get_fact(ROBOT_ID, "robot", "name")
    assert stored is not None
    assert stored.value == "Nilo"
    assert stored.metadata["disputed"][-1]["value"] == "Ziggy"


async def test_a_summarizer_that_fails_does_not_stop_the_rules(store):
    memory = RobotMemory(ROBOT_ID, store)

    async def broken(episodes):
        raise RuntimeError("the model timed out")

    memory.consolidator = Consolidator(store, summarizer=broken)
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await memory.met_person("ahmad", display_name="Ahmad")
    result = await memory.consolidate()
    assert result.facts_written >= 1


# -- privacy -------------------------------------------------------------------------------------------------------


async def test_one_memory_can_be_deleted(memory):
    written = await memory.remember("forget me")
    assert await memory.forget(MemoryKind.EPISODIC, written.id) is True
    assert await memory.forget(MemoryKind.EPISODIC, written.id) is False
    assert await memory.episodes() == []


async def test_deleting_a_person_removes_everything_about_them(memory):
    await memory.met_person("ahmad", display_name="Ahmad")
    await memory.remember("played with ahmad", person_id="ahmad")
    await memory.learn("ahmad", "enjoys", "cubes")
    await memory.remember("unrelated memory")

    removed = await memory.forget_person("ahmad")
    assert removed >= 3
    assert await memory.person("ahmad") is None
    assert await memory.episodes(person_id="ahmad") == []
    assert await memory.facts(subject="ahmad") == []
    assert [e.summary for e in await memory.episodes()] == ["unrelated memory"]


async def test_clearing_a_robot_leaves_other_robots_alone(store):
    mine = RobotMemory(ROBOT_ID, store)
    theirs = RobotMemory("other-robot", store)
    await mine.remember("mine")
    await theirs.remember("theirs")

    removed = await mine.clear()
    assert removed == 1
    assert await mine.episodes() == []
    assert [e.summary for e in await theirs.episodes()] == ["theirs"]


async def test_clearing_also_drops_the_current_exchange(memory):
    memory.working.add("something in flight")
    await memory.clear()
    assert len(memory.working) == 0


async def test_counts_report_every_store(memory):
    await memory.met_person("ahmad")
    await memory.learn("ahmad", "enjoys", "cubes")
    memory.working.add("hello")
    counts = await memory.counts()
    assert counts == {"episodes": 1, "facts": 1, "people": 1, "working": 1}


# -- restart -------------------------------------------------------------------------------------------------------------


async def test_memory_survives_a_restart(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"

    first_store = SqliteMemoryStore(path)
    first = RobotMemory(ROBOT_ID, first_store)
    await first.open()
    await first.met_person("ahmad", display_name="Ahmad")
    await first.remember("played with the cube", person_id="ahmad", importance=0.8)
    await first.learn("ahmad", "enjoys", "cubes", confidence=0.9)
    await first.aclose()
    await first_store.aclose()

    second_store = SqliteMemoryStore(path)
    second = RobotMemory(ROBOT_ID, second_store)
    await second.open()
    try:
        person = await second.person("ahmad")
        assert person is not None and person.display_name == "Ahmad"
        assert [e.summary for e in await second.episodes()] == ["played with the cube", "met Ahmad"]
        assert [f.value for f in await second.facts(subject="ahmad")] == ["cubes"]
        assert await second.recall("cube", person_id="ahmad")
    finally:
        await second.aclose()
        await second_store.aclose()


async def test_consolidation_state_survives_a_restart(tmp_path: Path):
    path = tmp_path / "memory.sqlite3"
    first_store = SqliteMemoryStore(path)
    first = RobotMemory(ROBOT_ID, first_store)
    await first.open()
    for _ in range(DEFAULT_PATTERN_THRESHOLD):
        await first.met_person("ahmad", display_name="Ahmad")
    await first.consolidate()
    await first_store.aclose()

    second_store = SqliteMemoryStore(path)
    second = RobotMemory(ROBOT_ID, second_store)
    await second.open()
    try:
        # Everything was already folded in, so a fresh process does not re-derive it.
        assert (await second.consolidate()).episodes_read == 0
    finally:
        await second_store.aclose()


# -- the retriever on its own ------------------------------------------------------------------------------------------------


async def test_the_retriever_can_be_used_without_the_service(store):
    await store.add_episode(episode("a thing happened", timestamp=at(-10)))
    retriever = MemoryRetriever(store)
    selected = await retriever.retrieve(MemoryQuery(robot_id=ROBOT_ID, text="thing"))
    assert [item.text for item in selected][0].endswith("a thing happened")


async def test_a_trivial_memory_loses_to_an_important_one(memory):
    await memory.remember("trivial", importance=IMPORTANCE_TRIVIAL, timestamp=at(-60))
    await memory.remember("important", importance=IMPORTANCE_CRITICAL, timestamp=at(-60))
    ranked = [item.text for item in await memory.recall("")]
    assert ranked.index("observation: important") < ranked.index("observation: trivial")
