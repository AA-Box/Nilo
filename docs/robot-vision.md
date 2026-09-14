# Robot vision

Snapshots in, tracked entities and events out — with nothing heavyweight required to run
it, and nothing in it able to command the robot.

Related: [behavior-system.md](behavior-system.md) (what reads the world model vision writes),
[robot-simulator.md](robot-simulator.md) (where the test frames come from),
[safety-model.md](safety-model.md) (why perception is never a safety input on its own).

---

## 1. The pipeline

```
camera capture → decode → preprocess → detect → recognize → track/associate
                                                                   ↓
                                             world model ← entities, events
```

One call, one frame:

```python
from robot.vision import VisionPipeline

pipeline = runtime.vision("nilo-sim-01")       # wired to the device's camera tool
result = await pipeline.process()
result.detections                              # what this frame held
result.latency_ms                              # how long the pass took
```

**Snapshots, not video.** Continuous perception is a timer over `process()`
(`pipeline.start(interval_s=1.0)`), never a requirement. The device already has a capture
tool, a still image is what a vision endpoint accepts, and a design that needs a frame
stream cannot run on hardware that cannot send one.

**A failure is a result, not an exception.** A detector that raises, a camera that returns
nothing, a model file that moved — each produces a `VisionResult` with an `error`, a
published `VisionFrameProcessed` event, and a world model left exactly as it was. In
particular a failed detection does **not** produce "nobody is here": a person must not
vanish from the world because a weight file was deleted.

---

## 2. The interfaces

| Role | Protocol | Ships with |
|---|---|---|
| where a frame comes from | `FrameSource` | `McpFrameSource` (the device tool), `StaticFrameSource` (fixtures), `CallableFrameSource` |
| decode and preprocess | `VisionProvider` | `HeaderVisionProvider` (no dependencies), `OpenCVVisionProvider` |
| find things | `ObjectDetector` | `NullDetector`, `FakeDetector`, `YoloObjectDetector` |
| find faces | `FaceDetector` | `NullDetector`, `FakeDetector` |
| name a face | `FaceRecognizer` | `EmbeddingFaceRecognizer` |
| keep ids across frames | `Tracker` | `CentroidTracker` |

Each is separate because they fail, scale and get replaced separately: YOLO for objects,
nothing for faces, and a fixture source for frames is a perfectly reasonable configuration,
and none of the three has to know about the others.

### Nothing heavyweight is required

* `HeaderVisionProvider` reads dimensions out of the PNG/JPEG header in pure Python. And
  since coordinates are normalized, **nothing downstream needs the resolution at all**.
* `NullDetector` finds nothing, successfully. A deployment with no model gets a pipeline
  that runs, a world model that stays empty, and behaviours that quietly do not fire. A
  robot with no detector should be boring, not broken.
* `OpenCVVisionProvider` and `YoloObjectDetector` import `cv2` / `ultralytics` **lazily,
  inside the one function that needs them**. A machine with neither still imports
  `robot.vision` for free, and a missing package is a typed `VisionUnavailable` rather
  than an ImportError at startup.
* `FakeDetector` returns a script. Every vision test in the repository runs against it.

---

## 3. Coordinates are normalized

A `BoundingBox` is 0.0–1.0 in image space, never pixels. `BoundingBox.from_pixels(...)` is
the one place a resolution appears, and it is at the detector boundary.

That makes a tracker written against the simulator's 160×120 frame work unchanged on a
1600×1200 camera, makes "the person is on the left" a comparison against `0.5`, and means
a frame whose dimensions could not be read is still perfectly usable.

`position_from_box()` turns a box into a rough metric `Position`. The bearing is
trustworthy (off-centre fraction × field of view). The **distance is not**: one camera
cannot measure distance, so it compares apparent size against a reference and says so in
its docstring. Behaviours use it for "further than 900 mm", never for anything that has to
be right.

---

## 4. Tracking

A detector answers "what is in this image". A tracker answers "is that the same person as
last time" — which is the question behaviours actually ask, because a greeting that fires
once per frame is not a greeting.

`CentroidTracker` associates greedily by IoU with a centre-distance fallback, allocates ids
per kind (`person-1`, `object-2`), and drops a track after `max_misses` consecutive frames
without a sighting. Matching runs best-score-first, so the assignment does not depend on
the order the detector happened to return boxes in — the difference between a deterministic
tracker and one that renumbers people at random.

A track carries `hits`, `misses`, a bounded centre history, `direction()` (`left` /
`right` / `still`) and `approaching()`.

---

## 5. Events

| Event | When |
|---|---|
| `PersonDetected` | a person track is confirmed, once per track |
| `PersonLost` | that track went unseen past the timeout |
| `FaceDetected` | a face track is confirmed |
| `KnownPersonRecognized` | that face matched somebody in the registry |
| `UnknownPersonDetected` | it did not. Not an error: most faces are strangers |
| `ObjectDetected` / `ObjectLost` | the same, for objects |
| `VisionFrameProcessed` | every pass, with its latency — success or failure |

Announcements are once per track, in track-id order, so a subscriber cannot come to depend
on detector ordering.

---

## 6. Face identity, and where it lives

```python
FaceIdentity(person_id="ahmad", display_name="Ahmad", embedding_ref="9f2c...", confidence=0.91)
```

`embedding_ref` is a **reference**, not a vector. Embeddings live in the `FaceRegistry`,
and what travels through events and into the world model is an id. Biometric data then has
one home and one lifetime, which is what makes `registry.forget(person_id)` a promise that
can actually be kept.

Learning new faces is off by default: `EmbeddingFaceRecognizer(registry,
enroll_unknown=True)` is an explicit decision about storing biometric data, not a default
somebody inherits.

---

## 7. Frame retention

| Mode | What happens to the image bytes |
|---|---|
| `EPHEMERAL` (**default**) | dropped the moment detection finishes; nothing kept |
| `LAST_ONLY` | the most recent frame stays in memory, for debugging |
| `PERSIST` | written to a directory — only ever an explicit operator choice |

A camera frame is a photograph of somebody's home. Keeping one is a decision somebody
makes, not a default they inherit. `PERSIST` without a directory keeps nothing and says so
in the log rather than guessing a path.

---

## 8. Looking at, and following

Vision produces coordinates; `robot/behavior/tracking.py` turns them into commands. It
lives in the behaviour layer because it *commands*, and perception is not allowed to —
`robot/vision/` may not import `robot/actions/` at all, and the import table is a test.

Both controllers are pure decision functions, which is what makes "the head does not
jitter" an exact test:

```python
decision = decide_look_at(point, now=..., last_command_at=..., tuning=tuning)
decision = decide_follow(offset_x=..., distance_mm=..., now=..., last_command_at=..., tuning=tuning)
```

Three rules keep a camera-driven controller from shaking the robot apart:

* **Deadband** — an error smaller than the deadband is noise, and nothing is commanded.
* **Rate limit** — at most one command per interval, whatever perception reports in
  between. A fast tracker must not become a command stream the head cannot follow.
* **Proportional steps with a ceiling** — command a fraction of the error (`look_at_gain`,
  `follow_turn_gain`), never all of it, and never more than the maximum step. Commanding
  the whole error is what makes tracking oscillate.

Following is **deterministic and bounded**, one leg at a time: turn towards the target
(capped), drive towards the stop distance (capped), hold station inside a hysteresis band,
back off if the target is too close, and stop outright if a sensor says the way is blocked.
No LLM is involved, and every leg goes through the action layer and its safety policy like
any other command. Every number lives in `BehaviorTuning`.

When the world holds a target with no image point — a target id from the device rather
than from this pipeline — `follow_person` hands it to the device's own follow tool instead,
which closes the loop faster than a round trip through this process can.

---

## 9. The simulator, and the fixtures

Scenarios that exercise perception (`python -m robot.simulator --scenario <name>`):

| Scenario | What happens |
|---|---|
| `person_enters_room` | somebody walks in |
| `person_moves_across` | left to right across the field of view, then back |
| `person_approaches` | walks towards the robot and stops close |
| `person_leaves_room` | somebody who was there walks out |
| `object_appears` | a cube is put down, moved, then taken away |

Committed frames live in `main/nilo-server/tests/robot/fixtures/vision/` — eight small PNGs
rendered by the simulator's own camera (an empty room, a person left/centre/right/close, a
person with an object, an object alone, the dock). Regenerate them with
`python ../../scripts/make_vision_fixtures.py` from `main/nilo-server`. They are a few
hundred bytes each, contain no photographs of anybody, and let a vision test assert on real
image bytes with no camera and no network.

---

## 10. Metrics

`pipeline.metrics` counts frames, detections and failures, and tracks mean, max and last
latency in milliseconds. `metrics.snapshot()` returns plain numbers for an endpoint or a
log line, and every pass publishes `VisionFrameProcessed` with its own latency — including
the passes that failed, because "perception stopped answering" and "perception answered
with nothing" look identical from the world model otherwise.

---

## 11. What vision is not

It is not a safety input. The safety policy gates motion on range and cliff **sensors**,
not on whether a detector saw something ([safety-model.md](safety-model.md)). A missed detection must
never be the only thing standing between a robot and a staircase, and in this design it
never is.
