# Personality and internal state

What makes one robot consistently different from another, and what makes it feel alive
between LLM calls — including when there are none.

**A note on language, up front.** This page and the code it describes talk about
*internal behaviour-control variables*, not emotions. The robot does not feel anything.
`valence` rising after a greeting means a number went up and some behaviours became more
likely. Every log line, docstring and comment in `robot/personality/` is written that way
deliberately: a system that claims feelings it does not have is lying to the person who
owns it, and that is not a trade we make for charm.

Related: [behavior-system.md](behavior-system.md) (what reads these numbers),
[robot-animation.md](robot-animation.md) (what the energy variable gates),
[safety-model.md](safety-model.md) (the layer none of this can reach).

---

## 1. Two halves, two lifetimes

| | Traits | Control variables |
|---|---|---|
| Where | `robot/personality/traits.py` | `robot/personality/emotion.py` |
| Change | rarely, deliberately | constantly, on their own |
| Persisted | yes, on change | coarsely, and only occasionally |
| Set by | config, preset, owner | decay plus what happens |

### Traits

Six numbers, all normalized 0..1, all frozen:

| Trait | Raises | Also does |
|---|---|---|
| `sociability` | greeting, following, approaching | sets where `social_need` rests |
| `curiosity` | investigating, looking around, exploring | sets where `curiosity` rests |
| `playfulness` | bigger, more embellished animations | lifts the resting mood slightly |
| `boldness` | approaching, exploring | sets how far confidence recovers |
| `patience` | — | lengthens the boredom climb |
| `energy_baseline` | — | sets where `energy` rests when rested |

Four presets exist (`balanced`, `puppy`, `cat`, `assistant`) and a YAML file can seed from
one and override it:

```yaml
# data/robot_personality.yaml
personality:
  preset: puppy
  patience: 0.6        # a puppy that can sit still
```

### Control variables

Seven numbers, also 0..1: `valence`, `arousal`, `curiosity`, `boredom`, `social_need`,
`confidence`, `energy`.

Each **decays exponentially towards a baseline** derived from the traits, with its own
half-life, and is **nudged by named stimuli**. Nothing snaps; nothing is linear (a linear
approach reaches the baseline and sits exactly on it, which reads as a robot that stopped).

| What happens | What moves |
|---|---|
| a positive interaction | `valence` up, `social_need` down, `boredom` reset |
| nothing at all | `boredom` climbs, `curiosity` climbs towards the trait |
| a motion fails | `confidence` down a little |
| charging | `energy` climbs on a much faster half-life |
| an unexpected obstacle | `arousal` up |
| being touched | `valence` up, `social_need` down, `boredom` reset |

Boredom is the one that rises when nothing happens, and it is not a special case: its
baseline is 1.0, so with no stimulus it climbs and every stimulus knocks it back. Patience
scales the half-life, so a patient robot climbs more slowly — same ceiling, longer fuse.

Every number, half-life and nudge lives in `EmotionTuning`. There are no constants in the
logic.

---

## 2. Wiring

```python
from robot.personality import PersonalityModel, load_traits

personality = PersonalityModel("nilo-sim-01", traits=load_traits(), store=store)
personality.attach(runtime.events)     # events now move the variables
engine.set_drives(personality)         # the behaviour engine reads it each tick
```

`runtime.personality(robot_id)` does this for you, and `runtime.behavior(robot_id)` wires
the result into the scheduler and the animation engine's energy gate.

The model has no background task. The variables decay **on read**, which means there is
nothing to leak, nothing to schedule, and a decay of an hour costs the same as a decay of
a second.

Which event means what is a table (`DEFAULT_BEHAVIOR_STIMULI` plus the handlers in
`model.py`), so a deployment with its own behaviours extends the mapping rather than
editing the model. A hazard that stays asserted for ten frames is **one** surprise, not
ten: the sensor path is edge-triggered, or arousal would saturate on a single cliff.

---

## 3. Influence on behaviour

The behaviour engine reads the control variables as `Drives`
([behavior-system.md](behavior-system.md)). Traits reach behaviour **through** them: a
sociable robot rests at a higher `social_need`, and the greeting score reads `social_need`.
That is one mechanism rather than two, and it is why a trait change is visible over the
next few minutes rather than instantly.

Worked examples, from the tests:

* `curiosity` 0.05 vs 0.95 changes the `investigate_object` score, and therefore what the
  robot does in a room with a new cube in it.
* `sociability` 0.05 vs 0.95 changes the `greet_person` score.
* `energy` below the threshold suppresses every animation marked `energetic`, so a
  flat-battery robot greets you with `calm_greeting` instead of a wiggle.

---

## 4. What personality cannot do

It cannot touch safety. Not by scoring higher, not by a trait at 1.0, not by any
combination.

* `robot/personality/` may import `robot/state/` and `robot/events/`. It may **not** import
  `robot/actions/`, `robot/behavior/`, `robot/animation/` or `robot/safety/` — the import
  table is a test (`main/nilo-server/tests/robot/test_layering.py`).
* A test sweeps **every trait** across its full range (0.0, 0.25, 0.5, 0.75, 1.0) with a
  cliff asserted and submits a move: rejected every time, with
  `RejectionReason.CLIFF_HAZARD`, and nothing reaches the device.
* `boldness` raises how willing the robot is to *propose* driving somewhere. It does not
  widen a single motion limit. Boldness is a preference; limits are not negotiable.

---

## 5. Persistence

`PersonalityStore` writes one JSON file per robot, atomically (temporary file plus
`os.replace`), under `data/robot_personality/`:

* **Traits** are written whenever they change and read at startup, so a robot is the same
  robot after a restart.
* **The control variables** are snapshotted coarsely — two decimal places — at most once
  per throttle window, and on a clean shutdown. Writing them on every change would be a
  file write per tick, and restoring them exactly would resurrect a five-hour-old mood.
* A corrupt file is logged and treated as absent. A personality is a preference, not a
  safety input; it must never stop a robot from starting.

Persistence is **opt-in at the runtime level**: `RobotRuntime(personality_store=...)`. A
runtime built without one keeps personalities in memory, which is what the tests and the
CLI use — constructing a runtime must never write into `data/`.

To forget a robot entirely: `store.delete(robot_id)`.
