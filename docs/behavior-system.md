# The world model and the behaviour engine

What the robot does when nobody is telling it to — and, just as importantly, how to find
out **why** it did it.

The engine is a utility/score-based scheduler. It is deterministic, it runs on injected
time, and **no LLM is involved at any point**: not to choose a behaviour, not to arbitrate
between two, not to decide when to stop. A model that is down, slow, or absent from the
deployment changes nothing about the robot's autonomy. That is a requirement of the
architecture (robot-architecture.md Sect. 2.6), not a fallback.

Related pages: [robot-architecture.md](robot-architecture.md) for where this sits,
[robot-actions.md](robot-actions.md) for the layer underneath it,
[safety-model.md](safety-model.md) for the layer underneath *that*, which behaviours cannot reach.

---

## 1. The world model

`robot/state/models.py` holds what the robot says about **itself**. `robot/state/world.py`
holds what it believes about **everything else**.

```python
from robot.state.world import EntityType, WorldState, person

world = WorldState(robot_id="nilo-sim-01")
world = world.observe(person("person-1", name="Ahmad", known=True))
world = world.attend_to("person-1")
world.people          # (Entity(id='person-1', ...),)
world.attention       # the same entity
```

A `WorldState` carries:

| Field | What it holds |
|---|---|
| `entities` | every `Entity`, indexed by id; `robots`, `people`, `faces`, `objects`, `locations` and `obstacles` are views over it |
| `attention_target` | the entity id the robot is attending to, or `None` |
| `current_interaction` | the exchange in flight, or `None` |
| `last_interaction` | the one before it, with its `ended_at` |
| `environment` | ambient sound level and direction, light, a label |
| `telemetry` | the robot's own last-known battery, sensors, pose and motion |

An `Entity` is `id`, `type`, `attributes`, `confidence`, `first_seen`, `last_seen`, and
optionally a metric `position` (millimetres, degrees, robot-relative) or an `image_point`
(normalized 0.0–1.0 image space — resolution-independent by construction).

Three properties are load-bearing:

* **It is a cache and it says so.** Perception is late and sometimes wrong. Readers ask
  `world.seen_within(EntityType.PERSON, 5.0)` rather than trusting the map.
* **It is frozen.** Every mutation returns a new snapshot, so a scoring pass cannot be
  raced halfway through by a perception update. This is what makes deterministic scoring
  possible at all.
* **Events write it; nothing else does.** `WorldModel` (`robot/state/world_model.py`)
  subscribes to the bus and folds `RobotConnected`, `TelemetryUpdated`, `BatteryUpdated`,
  `PoseUpdated` and `SensorUpdated` into snapshots. `runtime.world` is the one instance.

### Decay

Entities age out on a documented schedule, and a test proves it
(`main/nilo-server/tests/robot/test_world.py`):

| Kind | Forgotten after | Confidence half-life |
|---|---|---|
| person | 20 s | 8 s |
| face | 10 s | 4 s |
| object | 60 s | 30 s |
| obstacle | 30 s | 10 s |
| robot | 60 s | 30 s |
| location | 1 h | 30 min |

Confidence halves rather than dropping to zero: a person who stepped behind a chair is
probably still there. A behaviour that needs certainty asks for a recent sighting instead.

---

## 2. What a behaviour is

```python
class Behavior:
    name: str
    category: BehaviorCategory          # SAFETY | SYSTEM | SOCIAL | EXPLORATION | ENTERTAINMENT | IDLE
    priority: BehaviorPriority          # IDLE 0 | LOW 10 | NORMAL 50 | HIGH 80 | CRITICAL 100
    required_resources: frozenset[Resource]
    cooldown_s: float
    min_autonomy: AutonomyMode
    interruptible: bool
    min_runtime_s: float | None
    max_runtime_s: float | None

    def can_run(self, context) -> bool: ...      # a hard filter
    def score(self, context) -> float: ...       # utility in 0..1, pure
    async def execute(self, context) -> BehaviorResult: ...
    async def cancel(self) -> None: ...
```

`score` is **pure**. It may not await, may not command the robot, and may not read a
clock: the only time it sees is `context.now` and the only randomness `context.rng`. That
restriction is the whole reason a fixed world plus a fixed seed produces the same decision
1000 times out of 1000.

`execute` commands the robot through `context.robot`, which is the semantic action API
(`move`, `turn`, `look_at`, `set_expression`, `play_animation`, `follow`) and nothing else.
No behaviour can reach a motor, a servo or a PWM duty cycle — the vocabulary is the
boundary ([robot-architecture.md](robot-architecture.md) Sect. 3).

---

## 3. How the winner is chosen

One pass of `BehaviorScheduler.tick()`:

```
filter      autonomy mode → cooldown → resource conflict with what is running → can_run
score       every survivor, plus seeded jitter                    (pure, no awaits)
arbitrate   rank by (priority band, utility); preempt if it is worth it
execute     start the winner in its own task
```

The arbitration rules, in order:

1. A candidate below `min_score` is not worth doing.
2. **A higher priority band wins outright.** This is the categorical override: docking at
   8% battery is not compared against a greeting on utility, it wins because of what it
   is. Safety and system behaviours sit in bands nothing social may enter.
3. Within a band, higher utility wins.
4. A challenger preempts a *running* behaviour only if that behaviour is interruptible,
   has had its `min_runtime_s`, and is beaten by more than `preemption_margin` — or
   outranks it on band, which needs no margin.
5. A behaviour past its `max_runtime_s` is cancelled regardless.

Two behaviours that declare the same `Resource` never run at once — the same rule the
action queue enforces one layer down, applied early so the loser is never even scored.

### Determinism

Randomness comes from exactly one place: `random.Random(f"{seed}:{tick}:{name}")`. No wall
clock, no `hash()` (which is salted per process), no dict ordering — the registry iterates
sorted by name for that reason. Two schedulers with the same seed, tick and world select
the same behaviour on every machine; two with different seeds break ties differently,
which is what stops a room full of robots behaving in lockstep.

Time is injected as a callable (`clock=`), so the test for "boredom rises over five
minutes" runs in microseconds.

---

## 4. The behaviours that ship

| Behaviour | Band | Resources | From mode | Scores on |
|---|---|---|---|---|
| `go_to_charger` | CRITICAL | drive | NORMAL | battery below the low line, rising to ~1.0 at critical |
| `charging` | CRITICAL | display | PASSIVE | being on the dock, until charged enough |
| `low_battery` | HIGH | display | PASSIVE | low battery with no dock known |
| `sleep` | HIGH | display | PASSIVE | a long silence |
| `wake` | HIGH | display | PASSIVE | a stimulus while asleep — outranks sleep |
| `react_to_touch` | HIGH | display | PASSIVE | the touch sensor |
| `greet_person` | NORMAL | display, audio | PASSIVE | a fresh person, familiarity, social need |
| `look_at_person` | NORMAL | head | NORMAL | how far off-centre they are |
| `react_to_sound` | NORMAL | head, display | NORMAL | sound level above the threshold |
| `approach_person` | NORMAL | drive | FULL | distance beyond the approach minimum |
| `follow_person` | NORMAL | drive, head | FULL | a tracked person with a position |
| `investigate_object` | LOW | head | NORMAL | an unexamined object, scaled by curiosity |
| `look_around` | LOW | head | NORMAL | curiosity and boredom, halved when somebody is here |
| `explore` | LOW | drive | FULL | curiosity and boredom, in an empty room |
| `bored` | LOW | display | PASSIVE | time since the last interaction, ramping |
| `idle` | IDLE | — | PASSIVE | the floor: always available, always nearly worthless |

Worked examples, exactly as the design states them:

* `go_to_charger` — battery below 10% scores 0.99 **and** sits a band above everything
  social, so a familiar person in the room does not delay it.
* `greet_person` — a newly detected familiar person scores ~0.9; the same person a second
  later is not a candidate at all, because the per-person greet cooldown filters them out.
  "Recently greeted = near zero" is expressed as ineligibility rather than a small number,
  which is both truer and cheaper.
* `explore` — curiosity high and nothing important happening puts it in the middle of the
  pack, where a greeting or a touch beats it and idling does not.
* `bored` — zero before the onset, ramping linearly to its ceiling by `boredom_full_s`.

---

## 5. Autonomy modes

| Mode | May initiate |
|---|---|
| `OFF` | nothing. Direct commands only |
| `PASSIVE` | behaviours that claim only `DISPLAY` / `AUDIO` — reactive expressions, no movement |
| `NORMAL` | everything except the three that drive off on their own |
| `FULL` | everything, including `explore`, `approach_person`, `follow_person` |

The rule for `PASSIVE` is mechanical rather than a per-behaviour flag: a behaviour is
permitted if its declared resources are a subset of `{DISPLAY, AUDIO}`. A new behaviour is
therefore classified correctly by declaring what it commands, and a test asserts the whole
table for every built-in behaviour, in every mode
(`main/nilo-server/tests/robot/test_autonomy.py`).

Lowering the mode cancels anything the new mode would not have started:
`await engine.set_mode(AutonomyMode.PASSIVE)` stops a robot that is mid-drive.
`await runtime.set_autonomy(mode)` does it for every robot, and for the ones that connect
later.

The modes bound what the **engine** initiates. They are not a safety mechanism: a mode
cannot permit something the safety policy refuses, and `OFF` does not make an unsafe
command safe.

---

## 6. Tuning

Every number the engine compares against lives in `robot/behavior/tuning.py`, with a name,
a default, a unit in the name, and a comment saying what moving it does. No scoring
function contains a bare constant, and a test parses the source to prove it.

```yaml
# data/robot_behavior.yaml
behavior:
  greet_score: 0.9
  greet_cooldown_s: 300
  battery_critical_percent: 12
  score_jitter: 0.0        # fully replayable runs
```

```python
from robot.behavior import load_tuning
tuning = load_tuning()          # data/robot_behavior.yaml, or the built-in defaults
```

Like the safety limits, tuning is **not** read from the server config dict — in
manager-api mode that is replaced wholesale by the API response — and is constructed per
runtime rather than as a module singleton, so two robots in one process can be tuned
differently. An unknown key raises rather than being ignored: a typo that silently left
the defaults in place is the failure this arrangement exists to avoid.

---

## 7. "Why is the robot doing this?"

Every pass is published as structured debug events: `BehaviorEvaluated` (the full scoring
pass, including the candidates that were filtered out and why), then `BehaviorSelected`,
`BehaviorStarted`, and one of `BehaviorCompleted` / `BehaviorInterrupted`.

The same record is reachable synchronously — `engine.last_decision`, `engine.explain()`,
`engine.explain_data()` for JSON — and from the command line, with no server, no device
and no camera:

```bash
python -m robot.behavior explain --situation person_arrives
```

```
selected: greet_person
score: 0.93

alternatives:
  look_at_person: 0.44
  look_around: 0.10
  idle: 0.05

reasons:
  person person-1 newly detected
  familiar person
  greet cooldown expired

not eligible:
  go_to_charger: can_run said no
  explore: autonomy mode normal does not permit it
  ...
```

Other forms:

```bash
python -m robot.behavior situations                       # the built-in scenarios
python -m robot.behavior behaviors                        # names, bands, resources
python -m robot.behavior explain --situation low_battery --json
python -m robot.behavior explain --world snapshot.json --mode full --seed 7
```

The command **scores; it never commands**. It builds the engine with a robot handle that
records instead of acting, so asking a production configuration what it would do cannot
move anything.

---

## 8. Adding a behaviour

```python
from robot.behavior import Behavior, BehaviorCategory, BehaviorPriority, BehaviorResult
from robot.state.actions import Resource

class WatchTheDoor(Behavior):
    name = "watch_the_door"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.HEAD})

    def can_run(self, context) -> bool:
        return context.world.get("door") is not None

    def score(self, context) -> float:
        context.because("the door is where people come from")
        return context.tuning.look_at_person_score

    async def execute(self, context) -> BehaviorResult:
        record = await context.robot.head_angle(yaw_deg=0)
        return BehaviorResult.completed("watched the door", record)

engine.registry.register(WatchTheDoor())
```

The checklist:

* Declare **every** resource `execute` commands. That declaration is what stops two
  behaviours driving the same motor, and what classifies the behaviour for `PASSIVE`.
* Put new numbers in `BehaviorTuning`, not in the scoring function.
* Call `context.because(...)` for each thing that moved the score. It is what the explain
  output prints, and the difference between an answer and a number.
* Do not read the clock, sleep, or await inside `score`.
* Pick a band honestly. `CRITICAL` is for power and safety; a social behaviour that puts
  itself there is claiming it should interrupt a docking run.

---

## 9. What this layer cannot do

Behaviours propose. The action layer disposes, and the safety policy below it cannot be
reached from here: `robot/behavior/` may import `robot/actions/`, `robot/state/`,
`robot/personality/` and `robot/events/`, and the import table is a test
(`main/nilo-server/tests/robot/test_layering.py`). A behaviour scoring 1.0 still has its
move rejected on a cliff, at a stale sensor frame, or under an engaged emergency stop —
and the rejection reads the same whether a behaviour, the LLM or an operator asked
([safety-model.md](safety-model.md)).

Backend safety is a policy filter, never a guarantee. Nothing in this page changes that.
