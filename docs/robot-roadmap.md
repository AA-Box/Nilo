# Robot backend roadmap

Implementation phases and acceptance criteria for the architecture in
[`docs/robot-architecture.md`](robot-architecture.md).

Each phase is independently shippable and leaves the repository green. A phase is done when
**every** acceptance criterion is met — criteria are written to be mechanically checkable,
not to be argued about. Phases are ordered by dependency, not by how interesting they are:
the boring ones early (safety, registry, simulator) exist so the interesting ones later
(behaviour, personality) can be tested without hardware.

Two rules apply to every phase:

* **Upstream edit budget.** The total budget is 14 lines across 5 files
  (architecture §4.3). A phase that wants to exceed it must say so in its PR description
  and justify it. Adding a file to a new directory is always free.
* **No hardware in CI.** Every test runs against `robot/simulator/`. A test that needs a
  real robot is not a test, it is a bring-up procedure, and belongs in
  `docs/robot-bringup.md`.

---

## Phase 0 — Foundation (partly done)

Make the repository able to enforce its own rules before any robot code exists.

### Done in the architecture-audit change

* `main/xiaozhi-server/.ruff.toml` — repo-wide bug-only lint floor, green today.
* `main/xiaozhi-server/mypy.ini` — strict for `robot.*`, `ignore_errors` for upstream.
* `main/xiaozhi-server/requirements-dev.txt` — pinned dev tooling.
* CI: added a `lint` job; changed the test job from `pytest tests/test_smoke.py`
  (2 asserts) to `pytest -q` (65 tests); added `develop` to the push triggers; added a
  3.12 job against `requirements-dev.txt`.
* `Makefile`: `lint`, `lint-python`, `typecheck`.
* `plugins/register.py`: fixed `FunctionRegistry.__init__`'s undefined `setup_logging()`,
  plus 20 tests pinning the registry contract and the `Action`/`ToolType` codes that
  `ServerPluginExecutor` dispatches on.

### Remaining

| # | Task | Why |
|---|---|---|
| 0.1 | Delete the stale module-level skip in `tests/plugins_func/test_loadplugins.py:15-18`. Its stated reason (needs `data/.config.yaml`) is factually wrong — `plugins_func` is a compat shim over `plugins/`, which uses a config-free `SimpleLogger`. | A permanently green skip hiding a passing test is the rot pattern that will later hide a safety test. |
| 0.2 | Make `core/utils/{intent,llm,memory}.py` importable without `data/.config.yaml` by moving `setup_logging()` out of module scope, then un-skip `tests/core/utils/test_instance_creators.py`. | This is the one real upstream defect blocking tests. Costs ~3 lines in 3 files; worth exceeding the budget for. |
| 0.3 | Unpin `torch`/`torchaudio` to a version with cp312 wheels; move CI, `Dockerfile-server-base` and `README.md:743` to one Python version (3.12); collapse the two Python CI jobs. | Robot code targets 3.12; today the full-dependency job can only run 3.10. |
| 0.4 | Rewrite `main/xiaozhi-server/CLAUDE.md`. It never mentions the robot, the no-upstream-edits rule, `pytest`, `tests/` or the `Makefile`; it tells agents to use `uv run`; it points at `test_mcp_functions.py`, which does not exist, in three places. | Every agent reads it first and is actively misdirected into editing upstream files. |
| 0.5 | Move or rename `main/xiaozhi-server/test_plugin.py`. It matches `python_files = ["test_*.py"]` and is kept out of collection only by `testpaths = ["tests"]`; it runs imperatively at import and ends in `sys.exit(1)`. | Widening `testpaths` for `robot/tests/` — which Phase 1 does — would abort the whole run. |
| 0.6 | Add `robot/.ruff.toml` extending the root config with the full style/typing rule set, plus a `flake8-tidy-imports` banned-import rule enforcing the layering table in architecture §5 — in particular that no LLM-facing module may import a motion primitive. | The central safety invariant is currently enforced by nothing mechanical. |
| 0.7 | Gate `.github/workflows/docker-image.yml` on `github.event.workflow_run.conclusion == 'success'` and on the Tests workflow passing. | It currently publishes to ghcr.io even when the base-image build failed, and runs no tests first. |
| 0.8 | Prune `.cozmo/unmerged-prs/` from the working tree (keep `INDEX.md`). ~41 MB, dominated by one 40 MB patch, on every clone and every CI checkout. | Cost with no benefit; the patches already conflict with upstream main. |

### Acceptance criteria

* `make lint && make typecheck && make test-python` passes from a clean checkout after
  `pip install -r requirements-dev.txt`.
* CI runs lint, mypy, and the full pytest suite on **3.12** on both `main` and `develop`
  and on every PR.
* `pytest -q` reports **zero module-level skips**.
* `robot/.ruff.toml` exists and a deliberately-planted `from robot.devices.motor import …`
  in an LLM-facing module fails lint. Prove it with a commit that adds the violation,
  shows the failure, and reverts it.
* `CLAUDE.md` describes the robot subsystem, names `docs/robot-architecture.md`, and
  contains no command that does not work.

---

## Phase 1 — Skeleton, protocol, event bus, simulator

The smallest thing that can be tested end to end with no hardware and no LLM.

**Build:** `robot/{__init__,config,logging}.py`, `robot/protocol/`, `robot/events/`,
`robot/state/` (world model + freshness), `robot/simulator/` (a fake device that speaks the
Xiaozhi WebSocket + MCP protocol), `robot/tests/`.

**Key constraints** (architecture §4.1, §5):

* `robot/` imports nothing from `core/` at module scope. `robot/logging.py` uses stdlib
  logging; **never** `setup_logging()` at module scope.
* `robot/config.py` reads `data/.robot.yaml` — **not** the Xiaozhi config dict, which is
  discarded wholesale in manager-api mode (`config_loader.py:66-83`), taking any speed,
  acceleration or geofence limits with it.
* Every protocol message is a Pydantic model with an explicit `version` field and an ack.
  Unknown types are dropped silently upstream (`textMessageProcessor.py:35`), so firmware
  skew is otherwise invisible in the field.
* Event bus queues are **bounded with drop-oldest**. The upstream audio queue is unbounded
  and backlogs silently under load (`connection.py:197`); do not copy that.
* No module-level mutable state. Every state object is constructible per test — the
  `output_counter` pattern (`core/utils/output_counter.py:5-7`) is what we are avoiding.

### Acceptance criteria

* `import robot` succeeds in a venv with **no** `data/.config.yaml` and **no** loguru
  configured. Asserted by a test that runs in a subprocess with a clean `cwd`.
* `mypy` passes at the strict `robot.*` settings, zero `# type: ignore`.
* The simulator connects to a running `app.py`, completes the Xiaozhi `hello` handshake,
  answers `initialize` and `tools/list`, and appears in the server log as a device with
  tools — verified by an integration test, not by hand.
* A protocol round-trip test: every message model serializes, deserializes, and rejects a
  wrong `version` with a typed error rather than an exception trace.
* An event-bus test proves drop-oldest under a producer faster than its consumer, and that
  a slow subscriber cannot block a fast one.
* World-state entries carry an age; a test proves that reading a stale entry past its
  freshness budget raises rather than returning stale data.

---

## Phase 2 — Device registry, session adapter, the bridge

Attach to the Xiaozhi server. First code that touches upstream.

**Build:** `robot/devices/`, `plugins/cozmo_bridge/__init__.py`. Spend the 2-line
`core/connection.py` budget (attach after `:264`, detach in the `finally` at `:308`, both
`try/except`-wrapped).

**Key constraints** (architecture §4.2, §4.3, R1–R5):

* All robot state on `conn` goes under **one** namespaced attribute. `ConnectionHandler`
  has no fixed shape and 13+ attributes are grafted on by other modules.
* The bridge **self-asserts** after registration — `scan_plugins` swallows every import
  exception behind a `print` (`plugins/__init__.py:100-108`), so a typo otherwise disables
  the whole robot subsystem while the server looks healthy.
* The bridge must not re-export any `BasePlugin` subclass (`register_plugins_to_conn`
  reflects over `dir(module)` with no dedup) and must use `if TYPE_CHECKING:` for
  `ConnectionHandler` (the class does not exist yet at `connection.py:87`).
* Never submit to `conn.executor`, never assume `conn.tts`/`conn.asr`/`conn.func_handler`
  are non-`None`, never cache `conn.logger` (it is swapped at `:663`).
* The registry holds weak references and tolerates double-detach — `close()` is reachable
  from six call sites with no idempotency flag, and double teardown is the normal flow.

### Acceptance criteria

* Connecting two simulated robots yields exactly two registry entries with distinct
  `device_id`s; disconnecting one leaves exactly one; disconnecting the same session twice
  is a no-op.
* A test kills a simulated device without a clean close (drop the TCP connection) and the
  registry entry is gone within a bounded time.
* `git diff --stat` against `develop` shows **at most 2 changed lines** in `core/`.
* The bridge's self-assertion fails the server start (loudly, with a non-zero exit or a
  startup error) when a robot module is deliberately broken. Tested.
* A normal (non-robot) Xiaozhi client still completes a full voice turn with the bridge
  loaded — the compatibility regression test, run in CI from Phase 2 onward.

---

## Phase 3 — Action system and safety policy

The core of the design rule. No LLM involved yet; actions are driven from tests.

**Build:** `robot/actions/` (model, lifecycle, queue, resource claims), `robot/safety/`
(policy, limits, watchdog, e-stop).

**Key constraints** (architecture §4.4, §4.5, R6, R7):

* Lifecycle is
  `PENDING → STARTING → RUNNING → SUCCEEDED | FAILED | CANCELLED | TIMED_OUT | REJECTED`.
  Illegal transitions raise.
* Resource claims over `DRIVE / HEAD / LIFT / DISPLAY / AUDIO / CAMERA`. Two actions
  claiming the same resource cannot run concurrently.
* Every semantic request is clamped against limits and sensor state **before** dispatch.
  `robot/safety/` is a policy filter, not a guarantee — it does not replace firmware.
* Every dispatch to the device passes an **explicit short timeout** to `call_mcp_tool`.
  The 30 s default is never overridden upstream (`mcp_executor.py:41`) and is two orders of
  magnitude too long for a motion acknowledgement.
* The watchdog runs on the **robot's own loop or thread**, never the Xiaozhi event loop,
  which runs ONNX inference, blocking HTTP, and a `gc.get_objects()` walk every 300 s.
* Personality and behaviour sit **above** safety in the layering and cannot weaken it.

### Acceptance criteria

* A property-based test over the lifecycle: no sequence of events reaches an illegal state
  or leaks a resource claim.
* A `move` beyond the configured limit is `REJECTED` with a typed reason, not clamped
  silently, and the rejection reaches the caller.
* A `move` with a cliff sensor asserted is `REJECTED` regardless of who requested it —
  parameterized over LLM-originated, behaviour-originated and API-originated requests.
* Watchdog: with the device link severed mid-action, the action reaches `TIMED_OUT` and a
  stop is attempted within the configured budget. Tested by having the simulator stop
  responding, not by mocking the clock away entirely.
* E-stop: from `robot/api/`, every queued action is `CANCELLED` and no new action is
  accepted until explicitly cleared.
* A test asserts that `robot/safety/` imports nothing from `robot/behavior/` or
  `robot/personality/` — enforced by the Phase 0.6 lint rule, and asserted again here.
* **Documented explicitly** in `docs/robot-safety.md`: which protections are duplicated in
  firmware and which exist only in the backend, and the failure mode of each when the
  Python process dies. Backend-only protections are, by definition, not guarantees.

---

## Phase 4 — Semantic action vocabulary (the LLM seam)

Expose actions to the LLM. This is where the critical design rule becomes real.

**Build:** `plugins/cozmo_bridge/tools.py`.

**Key constraints** (architecture §4.2 seam ③, §4.5, R8, R9):

* `@register_function(name, schema, ToolType.IOT_CTL)` — `IOT_CTL` is the **only** type
  auto-exposed to the LLM without editing `config.yaml`
  (`plugin_executor.py:99,102-118`), and the only branch that passes `conn` and awaits
  `async def` handlers. Import `ToolType` from `plugins.register`, **not** from
  `core.providers.tools.base` — same name, different enum.
* Every tool is namespaced `robot_*`. The tool namespace is flat and device-MCP tools
  silently win collisions (`unified_tool_manager.py:40-41`).
* Every handler validates, clamps via `robot/safety/`, enqueues, and **returns
  immediately** — `Action.NONE` for silent execution, `Action.RECORD` to log into history
  without a second LLM round-trip. Never block on hardware: `chat()` waits on each tool
  future with a 30 s timeout on the shared 5-worker pool, sequentially, with no
  cancellation.
* Deployments set `intent_type: function_call`. `intent_llm` caches its rendered tool
  prompt on a process-wide singleton at first use (`intent_llm.py:169`), so a second robot
  with different hardware is offered the first robot's tools.
* Vocabulary follows the `self.otto.action` pattern for the device side: integers with the
  unit in the parameter name (`distance_mm`, `angle_deg`, `speed_mmps`), allowed string
  values enumerated in the description, defaults on everything optional. Device MCP has
  three argument types — boolean, integer, string. No float, no enum.

### Acceptance criteria

* `robot_move`, `robot_turn`, `robot_look_at`, `robot_play_animation`, `robot_follow`,
  `robot_stop` appear in `func_handler.get_functions()` on a fresh connection **with no
  `config.yaml` edit**. Asserted by a test that builds a `UnifiedToolHandler` against a
  fake conn.
* A test enumerates every registered robot tool schema and asserts that no parameter name
  or description matches a raw-actuator denylist (`pwm`, `duty`, `servo_us`, `wheel_speed`,
  `voltage`, …). This is the mechanical form of the design rule.
* A test asserts that no robot tool schema contains a `number` type — device MCP cannot
  express it, so a float parameter is a latent bug.
* Every tool handler returns in **under 10 ms** with the motion layer stalled. Measured,
  not assumed.
* A collision test: when the simulated device advertises a tool named `robot_move`, the
  bridge detects the collision at session start and refuses, loudly, rather than letting the
  device tool shadow the guarded one.
* Total `tools/list` payload for the robot's device-side tools is **under 7970 bytes**, so
  it fits one `tools/list` page (`mcp_server.cc:483`).
* An end-to-end test with a scripted fake LLM: utterance in → tool call → clamped action →
  simulator executes → completion event → world state updated. No real LLM, no network.

---

## Phase 5 — Vision and world model

**Build:** `robot/vision/`, extend `robot/state/`.

**Key constraints** (architecture §2.4, R11):

* Coordinates are normalized 0.0–1.0 so tracking is resolution-independent.
* Do **not** route robot perception through `core/api/vision_handler.py`: it builds a VLLM
  provider per request, blocks the aiohttp loop for the model's full latency, and has the
  `web_test_client` auth bypass. Call `core.utils.vllm.create_instance` once from robot
  code and run `response()` in an executor.
* Copy the `{"action": "RESPONSE", …}` short-circuit convention (`mcp_executor.py:50-59`)
  for low-latency perception replies — it skips the second LLM pass entirely.
* The vision JWT is minted once per connection and never refreshed, so
  `self.camera.take_photo` returns 401 on sessions older than an hour. Either refresh it or
  document the ceiling.

### Acceptance criteria

* Delete the `web_test_client` bypass (part of the 14-line budget) and add a test that the
  endpoint rejects that `Client-Id`.
* Tracking tests run against recorded frames in `robot/tests/fixtures/`; no camera, no
  cloud, no network in CI.
* A target persists across frames with a stable id and decays out of the world model on a
  documented schedule; a test proves the decay.
* Vision never blocks the Xiaozhi event loop: a test asserts a voice turn completes within
  budget while a perception request is in flight.
* A session older than the JWT lifetime either still works (refresh implemented) or fails
  with a typed, logged error — not a bare 401 swallowed somewhere.

---

## Phase 6 — Behaviour engine and personality

**Build:** `robot/behavior/`, `robot/personality/`, `robot/animation/`.

**Key constraints** (architecture §4.4):

* Utility scoring must be **deterministic** given the same world state — seeded, no wall
  clock, no ambient randomness. Behaviour tests that flake are worse than no tests.
* The engine runs with **no LLM available**. This is a hard requirement, not a fallback.
* Animations are data-driven YAML; adding one requires no Python change.
* Personality influences scoring. It cannot influence safety. Enforced by layering.
* Emotion is an internal control signal. The upstream emoji-scrape
  (`textUtils.py:84-106`) fires at most once per turn, defaults to `happy`, and never fires
  on the `direct_answer` path — treat it as one weak input, never the source of truth.

### Acceptance criteria

* With the LLM provider hard-failing every call, the robot still selects and executes
  behaviours for a full simulated session. This is the headline test of the phase.
* Given a fixed world state and a fixed seed, the engine selects the same behaviour 1000
  times out of 1000.
* A new animation is added in a PR that touches **only** a YAML file, and it plays.
* A test sweeps every personality trait across its full range with a cliff asserted, and
  the action is `REJECTED` in every case.
* Four autonomy modes (`OFF / PASSIVE / NORMAL / FULL`) are implemented, and a test asserts
  what each may and may not initiate.

---

## Phase 7 — Management API, memory, multi-robot

**Build:** `robot/api/`, `robot/memory/`. Spend the 3-line `app.py` budget.

**Key constraints** (architecture §4.3, §4.6, R5, R11, R12):

* Own aiohttp app on its own port. Do **not** add routes to `core/http_server.py` — there
  is no route registry there (`:43-76`).
* The robot task gets a done-callback that escalates into the safety layer. `ws_task` and
  `ota_task` are created and never error-checked today, so a port conflict kills a server
  silently.
* Robot admin credentials are **distinct** from the device-token signing key. The OTA
  endpoint is an unauthenticated oracle that mints valid 30-day device tokens
  (`ota_handler.py:143-298`), so "authenticated WebSocket connection" never authorizes
  actuation.
* Construct TTS/LLM/memory **per device**; never call `initialize_modules(init_tts=True)`
  from robot code. The process-global instance cache hands two robots one TTS pipeline with
  shared queues, shared threads, and a shared `close()`.
* Robot persistence is its own locked, durable store with a real shutdown await — not
  `mem_local_short`'s unlocked read-modify-write of a shared YAML from an unjoined daemon
  thread.
* Do not reuse `ManageApiClient`; it is a global singleton whose `safe_close()` turns every
  later call into a silent `None`.

### Acceptance criteria

* The API enumerates connected robots, reports per-robot state, accepts a semantic action,
  and accepts an e-stop. Every endpoint has a test.
* An unauthenticated request to every mutating endpoint is rejected. A valid **device**
  token is rejected for admin endpoints.
* Two simulated robots run a full session concurrently with **no** cross-talk: separate
  TTS pipelines, separate memory `role_id`s, separate world state. Asserted, because R5
  says the naive path fails exactly here.
* Killing the robot API task causes a logged escalation and a safe state, not a silently
  degraded server.
* Robot state survives a server restart; a test writes, restarts the store, and reads back.

---

## Phase 8 — Hardware bring-up

Out of CI scope by definition. Tracked here so the boundary is explicit.

* `docs/robot-bringup.md`: firmware tool vocabulary, the calibration procedure, and the
  safety checklist to run before the first powered motion test.
* Calibration knobs are **config, not constants** — trims persisted like otto's
  `set_trim`/`get_trims`, the 60 ms frame duration and `PRE_BUFFER_COUNT`
  (`sendAudioHandle.py:16,19`), wheel diameter, gear ratio, sensor offsets. A real chassis,
  head and lift all drift; a model that cannot be tuned is wrong on real hardware.
* First powered test is on blocks, wheels off the ground, with a physical power cutoff
  within reach.

### Acceptance criteria

* Every firmware-side protection in `docs/robot-safety.md` is demonstrated on hardware and
  the result recorded: cliff, collision, acceleration limit, watchdog timeout on link loss,
  e-stop.
* Link loss during motion halts the robot with the backend process **killed with
  `SIGKILL`** — proving the guarantee does not depend on Python cleanup.

---

## Ordering and what can run in parallel

```
0 ──► 1 ──► 2 ──► 3 ──► 4 ──► 6
           │      │      │
           │      └──────┴──► 7
           └──► 5 ──────────► 6
```

Phase 5 (vision) needs only Phase 1's world model and Phase 2's session adapter, so it can
run alongside Phases 3–4. Phase 6 needs both 4 and 5. Phase 7 needs 3 for e-stop and 4 for
the action vocabulary.

## Risks carried forward

| Risk | Phase it bites | Current mitigation |
|---|---|---|
| Robot firmware may not speak Xiaozhi MCP | 2, 4 | Fallback is a robot-owned WebSocket on its own port. Not `type:"robot"` on 8000 — that inherits the bind gate. |
| Upstream restructures `plugins/` or the text registry | 2 | Bridge self-assertion fails loudly rather than silently. The compatibility regression test catches it in CI. |
| Provider instance sharing (R5) | 7 | Per-device construction, asserted by the two-robot concurrency test. |
| `os._exit(0)` restart mid-motion (R6) | 3, 8 | Firmware watchdog. Proven in Phase 8 with `SIGKILL`. |
| Upstream merge conflicts in the 5 edited files | ongoing | 14 lines total, all `try/except`-wrapped, all documented in architecture §4.3 with rationale so a conflict is resolvable without re-deriving the reasoning. |
