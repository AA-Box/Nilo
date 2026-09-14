# nilo-server — notes for coding agents

This directory is the Nilo backend (Python 3.12, asyncio). The human-facing docs are the
source of truth; read them before touching anything:

* [`docs/architecture.md`](../../docs/architecture.md) — how the server works today
* [`docs/robot-architecture.md`](../../docs/robot-architecture.md) — where robot code goes and the safety rule
* [`docs/robot-domain.md`](../../docs/robot-domain.md) — the robot domain layer that is implemented
* [`docs/robot-actions.md`](../../docs/robot-actions.md) — the action and safety layers, and how to use them
* [`docs/safety-model.md`](../../docs/safety-model.md) — the safety split, and what firmware must implement itself
* [`docs/robot-simulator.md`](../../docs/robot-simulator.md) — the simulator: run it, extend it, test against it
* [`docs/robot-agent.md`](../../docs/robot-agent.md) — the LLM seam: tools, permissions, context, speech
* [`docs/robot-voice.md`](../../docs/robot-voice.md) — the voice loop: audio state, barge-in, expression
* [`docs/robot-api.md`](../../docs/robot-api.md) — the management API, its three gates, and the dashboard
* [`docs/architecture-final.md`](../../docs/architecture-final.md) — the whole system, every flow, every failure mode
* [`docs/robot-getting-started.md`](../../docs/robot-getting-started.md) — clone to a talking, moving, deciding robot
* [`docs/robot-protocol.md`](../../docs/robot-protocol.md) — every frame a device puts on the wire
* [`docs/observability.md`](../../docs/observability.md) — metrics, correlation ids, the trace
* [`docs/development.md`](../../docs/development.md) — setup, lint, tests, layout
* [`docs/upstream.md`](../../docs/upstream.md) — which code is inherited and how it is synced
* [`docs/upstream-changes.md`](../../docs/upstream-changes.md) — every hook into it, and a test that enforces the list
* [`docs/branding.md`](../../docs/branding.md) — naming rules (no product name in domain code, no Chinese)

## Layout in one glance

```
app.py            entry point
config/           YAML layering, NILO_* env overrides, placeholders, logging   (inherited, edited)
core/             session server, handlers, providers, tool system            (inherited — minimise edits)
plugins*/         interceptor plugins and @register_function tools             (inherited)
robot/            Nilo-owned code: protocol/ (routes), state/ (models + store + the action
                  vocabulary + the world model), events/ (bus), devices/ (robot registry,
                  MCP capability discovery), safety/ (limits, deterministic policy, e-stop,
                  watchdog), actions/ (the ten semantic actions, queue, registry, executor),
                  behavior/ (the utility-scored autonomy engine: tuning, scheduler, the
                  sixteen built-in behaviours, the explain CLI), personality/ (traits, the
                  internal control variables, the per-robot store), animation/ (YAML
                  animations and the engine that plays them), vision/ (the snapshot
                  perception pipeline: providers, tracker, faces, metrics), memory/ (working,
                  episodic, semantic and person memory over SQLite), agent/ (the LLM seam:
                  fourteen semantic tools, four permission classes, the runtime context, the
                  speech arbiter), voice/ (the complete loop: audio state, barge-in,
                  expression coordination, and the seam into the inherited session),
                  api/ (the management API and the
                  development dashboard, on their own port), telemetry.py (device
                  notifications -> world state), simulator/ (a fake robot on a real socket),
                  metrics.py + observability.py + correlation.py (one subscriber folds every
                  event into metrics and one trace line), runtime.py, session.py
tests/            pytest; tests/conftest.py points NILO_CONFIG at tests/fixtures/test_config.yaml.
                  tests/robot/ never opens a socket; tests/integration/ starts a real server;
                  tests/e2e/ is the ten scenarios, with local fakes for ASR, TTS, LLM and vision
```

Rules of thumb:

* New robot functionality goes under `robot/`; attach to `core/` through the smallest possible
  hook. Every line changed in `core/` is a line that conflicts on the next upstream port.
* The LLM never gets a tool that sets motor/servo/PWM values. Semantic actions only
  (`docs/robot-architecture.md`, `docs/safety-model.md`).
* Nothing talks to a device except `robot/actions/executor.py`. New capabilities are new
  action specs, not new call sites.
* `robot/safety/` may import `robot/state/` and nothing else from the subsystem, and it
  rejects rather than clamps. `tests/robot/test_layering.py` enforces the first;
  `tests/robot/test_safety.py` the second.
* The behaviour engine never consults an LLM, never reads the wall clock, and never writes
  a number into a scoring function — tuning lives in `robot/behavior/tuning.py` and a test
  parses the source to prove it (`docs/behavior-system.md`).
* Memory is four stores, not one vector index, and embeddings are optional. Nothing may
  overwrite a semantic fact blindly: every write goes through `merge_fact`, and an LLM that is
  less confident than what is stored does not change the answer (`docs/robot-memory.md`).
* The management API *can* move a robot, and it still has no second path to the hardware: every
  control request builds a typed action and submits it to the executor, `robot/api/` never calls
  `call_tool`, and it cannot import `robot/safety/`. Its token is never the device token, control
  endpoints are refused off loopback unless explicitly allowed, and it fails closed with no token
  (`docs/robot-api.md`).
* The agent owns conversation, interpretation, planning, tool selection and wording — and
  nothing else. Safety, behaviour scheduling, vision loops and timing have no LLM in them and
  keep working when the model is gone. Tool arguments are integers with the unit in the name,
  validated before submission, and every tool goes through the action executor
  (`docs/robot-agent.md`). One LLM stack: wrap `core/providers/llm/`, never add a second.
* Everything that makes the robot talk goes through `runtime.request_speech` and the speech
  arbiter. A background task that calls the model directly is a second mouth.
* The voice loop reuses the inherited VAD/ASR/TTS/Opus pipeline; it never reimplements one.
  Audio state is the single answer to "is this robot talking?", `SPEAKING -> LISTENING` is not
  a legal transition (a cut-off reply goes through `INTERRUPTED`), and the face changes as
  little as it can get away with (`docs/robot-voice.md`).
* Vision is snapshot-based and every provider is optional: OpenCV and Ultralytics are
  imported lazily and the defaults need nothing installed. Coordinates are normalized 0.0-1.0,
  never pixels, and frames are ephemeral by default (`docs/robot-vision.md`).
* Animations are YAML data under `robot/animation/library/`; adding one must never require a
  Python change (`docs/robot-animation.md`). Personality biases which action is proposed and
  can never reach safety — say "internal control variables", never "emotions"
  (`docs/robot-personality.md`).
* Backend safety is a policy filter, never a guarantee. Do not write a comment, log line or
  doc sentence implying the backend can stop a robot.
* Device routes live only in `robot/protocol/`; never spell a path into `core/`.
* The subsystem is **assembled in one place**: `robot/config.py` reads the hierarchy and
  `robot/bootstrap.py` turns it into a runtime, called once from `app.py`. A new configurable
  value is a field on a model there, never a sixth loader (`docs/configuration.md`).
* Every hook into inherited code is listed in `docs/upstream-changes.md` and enforced by
  `tests/robot/test_upstream_seam.py`. Adding one without documenting it fails CI.
* Metrics and the correlation trace are one subscriber over the existing events
  (`robot/observability.py`). Never instrument a subsystem directly (`docs/observability.md`).
* English only in comments, log lines and docs.

## Commands that work

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt   # full; on macOS vosk is skipped automatically
pytest -q                 # whole suite; the dev slice (requirements-dev.txt only) skips the socket tests
ruff check .
mypy                      # robot/ only, strict
python app.py             # needs data/.config.yaml or NILO_CONFIG=<file>; see docs/getting-started.md
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --scenario person_enters_room
python -m robot.simulator --status    # the simulated robot's own state
python -m robot.behavior explain --situation person_arrives   # why would it do that?
pytest tests/robot/test_memory.py -q  # memory: persistence, retrieval, consolidation, deletion
pytest tests/robot/test_agent.py -q   # the LLM seam, against a scripted model
pytest tests/robot/test_voice_loop.py -q  # the whole loop against a simulated robot
pytest tests/robot/test_api_control.py -q # the API that can move a robot, and its three gates
pytest tests/e2e -q                   # the ten end-to-end scenarios; writes tmp/e2e-report.md
NILO_ROBOT_ADMIN_TOKEN=dev python app.py  # dashboard on http://127.0.0.1:8010/
python ../../scripts/smoke_check.py   # against a running server
```
