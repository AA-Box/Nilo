"""Robot memory: four kinds, because remembering is four different problems.

    from robot.memory import RobotMemory, SqliteMemoryStore

    memory = RobotMemory("nilo-sim-01", SqliteMemoryStore("data/robot_memory.sqlite3"))
    await memory.open()
    await memory.met_person("ahmad", display_name="Ahmad")
    context = await memory.context_for("what do we usually play with?", person_id="ahmad")

``working``     the current exchange. Bounded, expiring, never persisted
``episodic``    things that happened, with a time and an importance
``semantic``    stable facts, with provenance and a confidence
``person``      who somebody is, and how well the robot knows them

**Not everything is a vector.** Embeddings are one optional signal
(:mod:`robot.memory.embeddings`); the robot works with them disabled, and a test proves
it. "When did I last see Ahmad" is a timestamp query and no amount of cosine similarity
improves it.

**Nothing overwrites a fact blindly.** Every write goes through
:func:`~robot.memory.store.merge_fact`: a matching value is a confirmation, a disagreeing
value only wins with higher confidence, and a losing claim is recorded as disputed. An LLM
may propose; it may not overwrite (docs/robot-memory.md).

**Retrieval is bounded.** A token budget, a ranked selection, and every returned item
carries the reasons it was chosen. The database never goes into the prompt.

Layering (docs/robot-architecture.md Sect. 7): this package may import ``robot/state``. It
may not import ``robot/actions`` or ``robot/behavior``.
"""

from robot.memory.consolidation import (
    DEFAULT_RULES,
    ConsolidationResult,
    ConsolidationRule,
    Consolidator,
    Summarizer,
)
from robot.memory.embeddings import (
    EmbeddingProvider,
    HashingEmbeddingProvider,
    NullEmbeddingProvider,
    cosine,
)
from robot.memory.models import (
    IMPORTANCE_CRITICAL,
    IMPORTANCE_NORMAL,
    IMPORTANCE_NOTABLE,
    IMPORTANCE_TRIVIAL,
    EpisodicMemory,
    EventType,
    MemoryKind,
    MemoryQuery,
    PersonRecord,
    Provenance,
    ScoredMemory,
    SemanticFact,
    approx_tokens,
)
from robot.memory.retrieval import (
    MemoryRetriever,
    RetrievalBudget,
    RetrievalWeights,
    lexical_overlap,
)
from robot.memory.service import RobotMemory
from robot.memory.store import (
    DEFAULT_DB_PATH,
    IN_MEMORY,
    MIGRATIONS,
    MemoryStore,
    SqliteMemoryStore,
    merge_fact,
)
from robot.memory.working import WorkingItem, WorkingMemory

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_RULES",
    "IMPORTANCE_CRITICAL",
    "IMPORTANCE_NORMAL",
    "IMPORTANCE_NOTABLE",
    "IMPORTANCE_TRIVIAL",
    "IN_MEMORY",
    "MIGRATIONS",
    "ConsolidationResult",
    "ConsolidationRule",
    "Consolidator",
    "EmbeddingProvider",
    "EpisodicMemory",
    "EventType",
    "HashingEmbeddingProvider",
    "MemoryKind",
    "MemoryQuery",
    "MemoryRetriever",
    "MemoryStore",
    "NullEmbeddingProvider",
    "PersonRecord",
    "Provenance",
    "RetrievalBudget",
    "RetrievalWeights",
    "RobotMemory",
    "ScoredMemory",
    "SemanticFact",
    "SqliteMemoryStore",
    "Summarizer",
    "WorkingItem",
    "WorkingMemory",
    "approx_tokens",
    "cosine",
    "lexical_overlap",
    "merge_fact",
]
