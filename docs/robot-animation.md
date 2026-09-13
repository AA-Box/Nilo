# Animations

How to make the robot do something expressive — **without writing Python**.

An animation is a YAML file. The engine reads it, plays it on a timeline, arbitrates
between animations that want the same part of the robot, and leaves the face somewhere
sensible when it ends. Adding one is adding a file; there is no registration call, no
import to edit, and no place to put a hardcoded `sleep`.

Related: [robot-behavior.md](robot-behavior.md) (what decides to play one),
[robot-personality.md](robot-personality.md) (what suppresses an energetic one),
[robot-actions.md](robot-actions.md) (the commands a step turns into).

---

## 1. Write one

Drop a file in `main/nilo-server/data/animations/` (or add it to the shipped set in
`main/nilo-server/robot/animation/library/`):

```yaml
name: victory_spin
description: A small celebration. Plays after something goes right.
priority: 55
energetic: true
transition: happy          # the expression the face is left in
tags: [celebration]
duration_ms: 1600          # optional: hold the final pose until here
steps:
  - {at_ms:    0, channel: eyes,  action: expression, args: {emotion: excited, intensity_pct: 100}}
  - {at_ms:  100, channel: body,  action: turn,       args: {angle_deg: 45}}
  - {at_ms:  600, channel: body,  action: turn,       args: {angle_deg: -45}}
  - {at_ms:  200, channel: audio, action: cue,        args: {sound: chirp}}
  - {at_ms: 1100, channel: head,  action: angle,      args: {pitch_deg: 12}}
```

That is the entire change. Restart the server (or call `load_library()` again) and

```python
await runtime.animations("nilo-sim-01").play("victory_spin")
```

plays it. A behaviour asks for it by name in exactly the same way.

`at_ms` is an **offset from the start**, not a delay from the previous step, so retiming
one step does not shift everything after it. Steps are sorted on load, so they can be
written in whatever order reads best.

One file may hold several animations, under an `animations:` key (the shipped files do
this), or a single animation as a top-level mapping, or a list. Names are unique across
the whole library: a duplicate is an error, because two files defining `greet` is somebody
copying when they meant to edit.

---

## 2. The channel vocabulary

| Channel | Actions | Arguments | Claims |
|---|---|---|---|
| `eyes` | `expression` | `emotion`, `intensity_pct` | `DISPLAY` |
| `eyes` | `look_at` | `x_pct`, `y_pct` | `DISPLAY` |
| `head` | `angle` | `pitch_deg`, `yaw_deg` | `HEAD` |
| `head` | `look_at` | `x_pct`, `y_pct` | `HEAD` |
| `lift` | `height` | `height_pct` | `LIFT` |
| `body` | `turn` | `angle_deg` | `DRIVE` |
| `body` | `move` | `distance_mm` | `DRIVE` |
| `audio` | `cue` | `sound` | `AUDIO` |

The vocabulary is closed. A step naming a channel or an action that does not exist fails
to load, loudly, at startup — rather than doing nothing on a robot at three in the
morning.

`emotion` must be one of the eleven semantic expressions: `neutral`, `happy`, `excited`,
`curious`, `confused`, `sad`, `sleepy`, `surprised`, `annoyed`, `scared`, `focused`.
Firmware maps each onto whatever its display can actually do; the backend never sends
pixels.

**Audio today.** The semantic action vocabulary has no audio cue yet (it arrives when
speech becomes an `AUDIO` resource claim — [robot-roadmap.md](robot-roadmap.md)). An
`audio` step is currently logged and skipped rather than silently pretended. Writing one
now is correct: it starts making a noise when the channel lands, with no change to the
file.

---

## 3. What the engine does with it

```python
engine = AnimationEngine("nilo-sim-01", player, load_library())
await engine.play("excited_greeting")
```

* **Timing.** Steps fire on their offsets; `duration_ms` holds the final pose. The clock
  and the sleeper are injected, which is why the test suite plays a four-second animation
  in microseconds and asserts the exact command order.
* **Resource ownership.** A playing animation owns the subsystems its channels map onto —
  the same ledger the action queue uses. Two animations that share no channels play
  together; two that share one do not.
* **Priority.** A higher-priority animation preempts a lower one and takes its resources.
  Equal or lower is **refused**, not queued: an animation that plays four seconds late is
  worse than one that never played. `play()` returns `None` for a refusal, and the reason
  is logged and published.
* **Looping.** `loop: true` repeats until something cancels it. A looping animation with
  no duration cannot busy-wait: the engine enforces a floor between passes.
* **Transitions.** `transition:` is the expression the face is left in when the animation
  ends — completed *or* cancelled. Without it, a preempted animation leaves the eyes
  mid-blink, which is the single most broken-looking thing a robot can do.
* **The energy gate.** `energetic: true` marks an animation big enough that a tired robot
  should skip it. Below the energy threshold it is suppressed, with a
  `low_energy` reason on the bus ([robot-personality.md](robot-personality.md)).
* **A step that fails is skipped**, not fatal: the rest of the animation still plays,
  because a robot frozen mid-gesture looks worse than one that drops a beat.

Every pass publishes `AnimationStarted`, then `AnimationFinished` or `AnimationCancelled`.

---

## 4. It is not a way around safety

An animation step becomes exactly the same semantic command a behaviour or an operator
would issue, through the same handle, attributed to the same source. Safety sees it,
clamps or rejects it, and the animation carries on with the next step.

There is no path from a YAML file to an actuator. `robot/animation/` may import
`robot/state/` and `robot/events/` and nothing else from the subsystem — the import table
is a test ([robot-architecture.md](robot-architecture.md) Sect. 7) — and it reaches the
action layer only through the `AnimationPlayer` protocol its caller supplies.

An animation that asks for a 45° turn on a robot whose limits permit 30° gets a
rejection, the same as anyone else would.

---

## 5. The shipped set

`main/nilo-server/robot/animation/library/` holds thirteen animations:

| File | Animations |
|---|---|
| `greetings.yaml` | `excited_greeting`, `calm_greeting`, `happy_wiggle`, `startled` |
| `idle.yaml` | `idle_breathe`, `look_around_sweep`, `curious_tilt`, `bored_sigh`, `confused_shrug`, `sleepy_settle`, `wake_stretch`, `charging_content`, `low_battery_warning` |

A deployment directory is loaded **after** the built-ins and may replace one by name, so
retuning `excited_greeting` for a chassis with no lift is a file in `data/animations/`
rather than a fork.

---

## 6. Checklist for a new animation

* Name it in `snake_case`; the name is the identity and is stable.
* Declare `transition:` if it touches the eyes. Otherwise the face is left wherever the
  last step put it.
* Mark it `energetic: true` if a flat-battery robot should skip it.
* Keep `loop: true` cheap, and give a looping animation a real `duration_ms`.
* Use offsets, not cumulative delays.
* Only claim the channels you actually command — the claim is what stops two animations
  fighting over the head.
