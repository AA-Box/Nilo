# Robot memory

Four kinds of memory, because remembering is four different problems — and a set of rules
about what is allowed to change what the robot believes.

Related: [robot-behavior.md](robot-behavior.md) (what reads the world model, which is a
different thing), [robot-vision.md](robot-vision.md) (where a `person_id` comes from),
[robot-architecture.md](robot-architecture.md) (where this sits).

---

## 1. Four stores

| Store | Holds | Lifetime | Persisted |
|---|---|---|---|
| **working** | the current exchange | minutes, bounded turns | no |
| **episodic** | things that happened | as long as they are worth keeping | yes |
| **semantic** | stable learned facts | until corrected | yes |
| **person** | who somebody is, and how well the robot knows them | until deleted | yes |

**Not everything is a vector.** Embeddings are one optional retrieval signal, and a poor
one for most of what a robot is actually asked: "when did I last see Ahmad" is a timestamp
query, "what is my name" is a fact lookup, and "what are we talking about right now" is a
buffer that should be thrown away shortly. Putting all four into one embedding index makes
each of them worse.

```python
from robot.memory import RobotMemory, SqliteMemoryStore

memory = RobotMemory("nilo-sim-01", SqliteMemoryStore("data/robot_memory.sqlite3"))
await memory.open()                                   # migrations run here

await memory.met_person("ahmad", display_name="Ahmad")
await memory.remember("played with the cube", event_type=EventType.PLAY, person_id="ahmad")
await memory.learn("ahmad", "enjoys", "cubes", confidence=0.9, learned_from="user")

context = await memory.context_for("what do we usually play with?", person_id="ahmad")
```

### Episodic

`id`, `robot_id`, `timestamp`, `event_type`, `summary`, `importance`, optional `person_id`,
and `metadata`. The summary is a sentence a person could read — episodic memory is what the
robot *recalls* about an event, not a transcript of it.

### Semantic

`subject` + `predicate` → `value`, with a `confidence` and a full `Provenance`. The
subject-predicate pair is the identity, so writing about the same pair updates the existing
fact rather than accumulating contradictions.

### Person

`person_id`, `display_name`, `first_seen`, `last_seen`, `interaction_count`, `familiarity`,
an optional `embedding_ref` (a reference into the face registry — never a vector), and a
small `facts` bag.

Familiarity rises asymptotically with interactions (the difference between the first and
second meeting matters far more than between the fiftieth and the fifty-first) and **decays
with absence**: `familiarity_now()` halves every thirty days, so somebody the robot has not
seen since spring is remembered without being treated as a regular.

### Working

Bounded turns, a TTL, and no persistence. Separate from "recent episodic memory" because
the two have different lifetimes and different truth: working memory may hold a
half-finished sentence and a guess about who is talking; episodic memory is what the robot
will still believe tomorrow.

---

## 2. Storage

SQLite through the stdlib `sqlite3` module, behind a `MemoryStore` interface.

**Why not an ORM.** The test suite runs against `requirements-dev.txt` alone (see the
`test-dev-slice` CI job), so a new runtime dependency would either break that job or make
robot memory untestable in it. What an ORM would buy is migrations and dialect
portability: the migration list is twenty lines, and the portability is what the interface
already provides. The cost of an ORM is paid by every deployment; the cost of this is paid
once, here.

**Postgres later is a new class, not a rewrite.** Every method on `MemoryStore` is a
coroutine taking and returning pydantic models or primitives — no cursor, no session, no
lazy relationship anywhere in the interface — and the one compound operation
(`upsert_fact`) is read-modify-write under a lock, which maps directly onto
`INSERT … ON CONFLICT DO UPDATE`.

**Migrations** are an ordered list of `(version, script)` pairs applied inside a
transaction and recorded in a `schema_version` table. Adding one is adding a tuple; never
edit one that has shipped. Opening a store runs them, idempotently.

**Nothing blocks the event loop.** `sqlite3` is synchronous and the session loop runs ONNX
inference and blocking HTTP, so every query goes through `asyncio.to_thread`, with one
connection (`check_same_thread=False`) and one lock. WAL is on, so the admin API reading
does not block the robot writing.

---

## 3. Retrieval, and the budget

**The database never goes into the prompt.** A retrieval pass returns a bounded, ranked,
explainable selection:

```python
selected = await memory.recall("what do we play with?", person_id="ahmad")
# → [ScoredMemory(kind=..., text=..., score=0.62, reasons=("about ahmad", "recent")), …]

context = await memory.context_for("…", person_id="ahmad", budget=RetrievalBudget(max_tokens=300))
```

Four signals, none of which is allowed to be the only one:

| Signal | What it is | Why it is not enough alone |
|---|---|---|
| relevance | lexical overlap, plus embedding similarity when enabled | misses "what happened just now" |
| recency | exponential decay, one-day half-life | misses what she told you last week |
| importance | what the writer said it was worth | returns the same five memories forever |
| association | the person the robot is talking to | says nothing about what was asked |

The budget is in **tokens**, not rows, because a row can be a sentence or a paragraph;
`max_items` is a secondary ceiling. Every returned item carries the reasons it was chosen,
so a prompt containing a strange memory can be asked why.

---

## 4. Embeddings are optional

```python
RobotMemory(robot_id, store)                                   # embeddings off — the default
RobotMemory(robot_id, store, embeddings=HashingEmbeddingProvider())   # on, no model needed
```

A robot must function with embeddings disabled, and that shapes the design: the default
provider produces nothing, every retrieval path works without a vector, and a test asserts
it. When enabled, embedding happens **once, on the way in** — never per query, where it
would be a model call per retrieval — and an embedding failure logs and stores the memory
anyway rather than losing it.

`HashingEmbeddingProvider` is a real, local, dependency-free provider: a hashed
bag-of-words projection. It is not a language model and does not pretend to be, but it is
deterministic, costs nothing, and makes the embeddings-enabled path testable in CI. A
deployment with a real model implements the same four-line protocol.

---

## 5. Consolidation, and who may change a fact

A background job reads unconsolidated episodes and writes semantic facts. It is
deterministic — the shipped rules are pattern matches, not a model — idempotent (folded
episodes are marked, so a restart does not re-derive a month of history), and bounded per
pass.

Shipped rules: people met repeatedly become `familiarity: familiar` and gain their name;
repeated play or praise about the same thing becomes `enjoys`; repeated failures about the
same subject become `often_fails`; places reached twice become `known_place`.

**The LLM does not get to overwrite facts.** A summarizer may be attached, and everything
it proposes goes through the same merge as any other claim:

| Situation | What happens |
|---|---|
| same value | a **confirmation** — confidence rises towards 1, provenance records it |
| different value, **higher** confidence | the new value wins, and `previous_value` is kept |
| different value, not higher | the stored value **stands**, and the attempt is recorded in `metadata.disputed` |

A model having an opinion is not the same as the robot learning something. A summarizer
that raises, hangs or returns nonsense does not stop the deterministic half from having
run.

**Provenance is written on every fact**: `learned_from`, the `source_ids` it was derived
from, `created_at`, `updated_at`, and how many times it has been confirmed. A fact with no
provenance could not be audited, corrected, or told apart from something invented.

---

## 6. Privacy

```python
await memory.forget(MemoryKind.EPISODIC, memory_id)   # one memory
await memory.forget_person("ahmad")                   # the person, their episodes, their facts
await memory.clear()                                  # everything this robot remembers
counts = await memory.counts()                        # what is stored, per kind
```

`forget_person` deletes the person record, **every episode about them**, and every fact
whose subject is them. A delete that leaves the episodes behind is worse than no delete,
because it looks like it worked. Clearing a robot also drops the current exchange.

Persistence is opt-in at the runtime level (`RobotRuntime(memory_store=…)`). A runtime
built without a store has no long-term memory rather than quietly creating a database
under `data/` — which is also why the test suite never writes one.

---

## 7. The admin API

Its own aiohttp app, on its own port, with its own credential.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/health` | liveness. No token, and it reveals nothing about any robot |
| `GET` | `/robots` | the robots this runtime knows |
| `GET` | `/robots/{id}` | connection state and memory counts |
| `GET` | `/robots/{id}/why` | the behaviour engine's decision, as JSON (`?fresh=true` re-scores) |
| `GET` | `/robots/{id}/memories` | episodes, facts, people, working — `?kind=`, `?person=`, `?limit=` |
| `GET` | `/robots/{id}/memories/recall` | what would go into a prompt for `?q=`, with scores and reasons |
| `POST` | `/robots/{id}/memories/consolidate` | run a consolidation pass now |
| `DELETE` | `/robots/{id}/memories/{kind}/{memory_id}` | delete one memory |
| `DELETE` | `/robots/{id}/people/{person_id}` | delete a person and everything about them |
| `DELETE` | `/robots/{id}/memories` | clear this robot's memory |

```bash
curl -H "Authorization: Bearer $NILO_ROBOT_ADMIN_TOKEN" \
     "http://127.0.0.1:8010/robots/nilo-sim-01/memories/recall?q=cube&person=ahmad"
```

* **Its own credential**, read from `NILO_ROBOT_ADMIN_TOKEN` — not from the server config
  dict, which manager-api mode replaces wholesale. It is distinct from the device-token
  signing key.
  The OTA endpoint is unauthenticated and, with auth enabled, will mint a valid token for
  whatever device id a caller asks for — so holding a valid *device* token authorizes
  nothing here ([safety.md](safety.md)). Comparison is constant-time; a misconfigured API
  with no token fails **closed**.
* **Its own port**, bound to loopback by default. Routes are never added to
  `core/http_server.py`, which serves device traffic.
* **It cannot move a robot.** There is no actuation endpoint, and a test asserts that no
  route name contains one. Actuation goes through the action layer, behind the safety
  policy; an admin surface that could drive a robot would be a second path to the hardware.

---

## 8. What memory is not

It is not the world model. `robot/state/world.py` is what the robot believes is around it
*right now*, with a decay measured in seconds; memory is what it will still know tomorrow.
They are deliberately separate, and memory never gates a motion:
`robot/memory/` may import `robot/state/` and nothing else from the subsystem — no actions,
no behaviour, no safety — and the import table is a test.
