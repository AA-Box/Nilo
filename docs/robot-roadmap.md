# Robot backend roadmap

Implementation phases and acceptance criteria for the architecture in
[robot-architecture.md](robot-architecture.md).

Nilo today is a voice/vision session backend with a device protocol registry. The robot
domain — actions, safety policy, world model, behaviour, personality, memory, simulator —
is **not implemented**. This page is the plan for building it, and the honest boundary
between what runs and what is design. Nothing below Phase 0 exists in the tree.

Each phase is independently shippable and leaves the repository green. A phase is done when
**every** acceptance criterion is met — criteria are written to be mechanically checkable,
not to be argued about. Phases are ordered by dependency, not by how interesting they are:
the boring ones early (safety, registry, simulator) exist so the interesting ones later
(behaviour, personality) can be tested without hardware.

**Notation.** A path in `backticks` is a file that exists in the tree today and can be
opened. A path in **bold** is a package this roadmap plans to create; it does not exist
yet. `python scripts/check_docs.py` fails on a backticked repository path that does not
exist, so the backticked half of that distinction is mechanical; the bold half is convention.

Two rules apply to every phase:

* **Inherited edit budget.** Most of `core/` is inherited from the upstream project and is
  re-synced (see [upstream.md](upstream.md)); every line changed there is a line that can
  conflict. The budget for the robot work is about 10 lines across 4 files
  (robot-architecture §4.2). A phase that wants to exceed it must say so in its PR
  description and justify it. Adding a file to a new directory is always free.
* **No hardware in CI.** Every test runs against the simulator. A test that needs a real
  robot is not a test, it is a bring-up procedure, and belongs with the Phase 8 bring-up
  notes.

---

## Phase 0 — Foundation

Make the repository able to enforce its own rules before any robot code exists.

### Done

The rebrand and the preceding audit closed most of this phase. Verified in the tree today:

| Item | Where |
|---|---|
| Server directory renamed to nilo-server; the Java manager modules are gone, so `main/` holds one component | `main/nilo-server`, [migration.md](migration.md) |
| Device protocol registry with one named protocol — `nilo` (`/nilo/v1/`, `/nilo/ota/`) — consumed by both servers instead of hard-coded paths, with `protocols.strict` (shipped `true`) rejecting any WebSocket path that matches no protocol | `robot/protocol/base.py:ProtocolRegistry`, `core/http_server.py:SimpleHttpServer._build_app`, `core/websocket_server.py:WebSocketServer._http_response` |
| `NILO_SERVER_HOST`, `NILO_SERVER_PORT`, `NILO_HTTP_PORT` and `NILO_LOG_LEVEL` override config keys, and `NILO_CONFIG` selects the user config file; no deprecated config aliases are left, so only the current key names load | `config/config_loader.py:ENV_OVERRIDES`, `config/config_loader.py:custom_config_path`, `config/config_loader.py:DEPRECATED_KEYS` |
| Module-level test skips removed. `pytest -q` collects and runs the suite; the only skips left are `pytest.importorskip` guards for the heavy runtime packages, so the dev-only dependency slice still runs | `tests/core/test_http_routes.py`, `tests/core/test_ws_path_gate.py`, `tests/plugins_func/test_loadplugins.py` |
| Tests no longer need a hand-written `data/.config.yaml`: `tests/conftest.py` points `NILO_CONFIG` at a committed fixture | `tests/conftest.py`, `tests/fixtures/test_config.yaml` |
| One Python version everywhere. CI runs 3.12 only, on `main`, `develop` and every PR, in four jobs: lint plus type-check plus docs check, full dependencies, the dev-only slice, and `docker compose config` | `.github/workflows/test.yml` |
| Repo-wide bug-only lint floor and strict mypy scoped to robot code | `.ruff.toml`, `mypy.ini` |
| `Makefile` targets: `test`, `lint`, `typecheck`, `check-docs`, `run`, `compose-validate`, `docker-build`, `smoke` | `Makefile` |
| `plugins/register.py` fixed (`FunctionRegistry.__init__` no longer calls an undefined `setup_logging()`); the registry contract and the `Action`/`ToolType` codes that `ServerPluginExecutor` dispatches on are pinned by tests | `plugins/register.py`, `tests/plugins/test_register.py` |
| Agent instructions rewritten: they name the robot subsystem, the docs, `pytest`, `ruff`, `mypy` and the no-inherited-edits rule, and every command in them runs | `main/nilo-server/CLAUDE.md` |
| The imperative `test_plugin.py` script at the server root is deleted, so widening `testpaths` for new test directories cannot abort a run | `pyproject.toml` |
| The archive of 26 unmerged upstream patches is pruned from the working tree | [migration.md](migration.md) |
| Branding and documentation are checked mechanically in CI: link resolution, backticked paths, `NILO_*` names, no Chinese text, legacy identifiers only where allowed | `scripts/check_docs.py`, `.github/workflows/test.yml` |

### Remaining

| # | Task | Why |
|---|---|---|
| 0.1 | Add a ruff config under **robot/** extending `.ruff.toml` with the full style/typing rule set, plus a `flake8-tidy-imports` banned-import rule enforcing the layering table in robot-architecture §7 — in particular that no LLM-facing module may import a motion primitive. | The central safety invariant is currently enforced by nothing mechanical. |
| 0.2 | Gate `.github/workflows/docker-image.yml` on the Tests workflow passing, not only on the base-image build. It already refuses to run when a `workflow_run` trigger reports a failed conclusion, but it still publishes to ghcr.io without running a test. | A release path that never runs tests will eventually ship a broken image. |
| 0.3 | Keep `make check-docs` at zero problems as pages are added and edited. It reports zero today and is already a required CI step. | A branding and link checker that is allowed to be red is not a check. |

### Acceptance criteria

* `make lint && make typecheck && make test && make check-docs` passes from a clean
  checkout after `pip install -r requirements-dev.txt`.
* CI runs lint, mypy, the docs check and the full pytest suite on **3.12** on both `main`
  and `develop` and on every PR.
* `pytest -q` reports zero module-level skips that are not `importorskip` guards on
  optional runtime packages.
* A ruff config under **robot/** exists and a deliberately-planted import of a motion
  primitive from an LLM-facing module fails lint. Prove it with a commit that adds the
  violation, shows the failure, and reverts it.
* No image is published from a commit whose Tests run did not pass.

---

## Phase 1 — Skeleton, config, event bus, world state, simulator

The smallest thing that can be tested end to end with no hardware and no LLM.
`robot/__init__.py` and `robot/protocol/` already exist and set the pattern.

**Build:** **robot/config.py**, `robot/logging.py`, `robot/events/`, `robot/state/`
(world model + freshness), **robot/simulator/** (a fake device that speaks the WebSocket
session protocol and device MCP), and tests under `tests/robot/`.

**Delivered** (see [robot-domain.md](robot-domain.md)): `robot/state/` (frozen domain models,
an age on every timestamped entry, `StaleStateError`, `RobotStateStore` plus an in-memory
implementation), `robot/events/` (typed events and a bounded drop-oldest async bus),
`robot/logging.py` (stdlib logging bridged into the server's sinks from `app.py`), and the
`tests/robot/` suite that covers them — including the subprocess test that proves `import
robot` needs no config file and pulls in no `core` module.

The **simulator is delivered** as well: `robot/simulator/` is a client that speaks the whole
device protocol — handshake, device MCP with paged `tools/list`, 15 published hardware
capabilities, motion that takes time, a configurable room with walls, obstacles, cliffs,
people and a dock, synthetic or fixture camera frames, telemetry notifications, failure
injection and a scenario runner on an accelerated or hand-driven clock. Its backend half,
`robot/telemetry.py`, turns `notifications/*` into world state and typed events. See
[robot-simulator.md](robot-simulator.md).

**Still open:** a robot config file and schema (**robot/config.py**), and typed robot protocol
messages with a version field and acks.

**Key constraints** (robot-architecture §4.1, §7):

* Robot code imports nothing from `core/` at module scope. The dependency runs the other
  way today — `config/logger.py` imports `robot.__version__` — so anything expensive at
  robot import time is paid by every process that logs.
* Robot logging uses stdlib logging; **never** call `setup_logging()` at module scope. That
  function reads the config file and configures loguru globally
  (`config/logger.py:setup_logging`).
* Robot config is its own file, not a slice of the server config dict. In manager-api mode
  the local config is replaced wholesale by the API response — only the `server` and
  `manager-api` blocks survive (`config/config_loader.py:get_config_from_api_async`) — so
  any speed, acceleration or geofence limit stored there would vanish in production.
* Every protocol message is a frozen Pydantic model with an explicit version field and an
  ack, in the style `robot/protocol/base.py:ProtocolSpec` already sets for route specs. A message of an unknown type is logged
  and dropped with no reply to the device
  (`core/handle/textMessageProcessor.py:TextMessageProcessor.process_message`), so without
  an ack, firmware skew is invisible from the device side.
* New message type names must not collide with the wire vocabulary already in use:
  `robot/protocol/nilo.py:RESERVED_MESSAGE_TYPES`.
* Event-bus queues are **bounded with drop-oldest**. The inherited audio queue is an
  unbounded `queue.Queue` (`core/connection.py:ConnectionHandler.__init__`, `asr_audio_queue`)
  and backlogs silently under load; do not copy that.
* No module-level mutable state. Every state object is constructible per test — the
  module-level dictionaries in `core/utils/output_counter.py` are what we are avoiding.

### Acceptance criteria

* `import robot` succeeds in a venv with **no** `data/.config.yaml`, no `NILO_CONFIG`, and
  no loguru configured. Asserted by a test that runs in a subprocess with a clean `cwd`.
* `mypy` passes at the strict `robot.*` settings, zero `# type: ignore`.
* **Done.** The simulator connects to a running `app.py`, completes the session handshake
  against the `nilo` route, answers `initialize` and `tools/list`, and appears in the server
  log as a device with tools — verified by `tests/integration/test_simulator_e2e.py`, not by
  hand.
* A protocol round-trip test: every message model serializes, deserializes, and rejects a
  wrong version with a typed error rather than an exception trace.
* An event-bus test proves drop-oldest under a producer faster than its consumer, and that
  a slow subscriber cannot block a fast one.
* World-state entries carry an age; a test proves that reading a stale entry past its
  freshness budget raises rather than returning stale data.

---

## Phase 2 — Device registry, session adapter, the bridge

Attach to the session server. First code that touches inherited files.

**Build:** `robot/devices/`, **plugins/robot_bridge/**. Spend the 2-line
`core/connection.py` budget: attach inside `ConnectionHandler.handle_connection` next to
the existing `register_plugins_to_conn(self)` call, detach in that method's `finally`
block, both `try/except`-wrapped.

**Delivered**: `robot/devices/` (the robot registry, the tool and capability registries and
MCP capability discovery), `robot/runtime.py` (the control plane) and `robot/session.py` (the
seam, which swallows its own failures instead of wrapping every call site). The
`core/connection.py` budget is spent: one import plus an `await` at each end of
`handle_connection`. Registration, reconnect, supersede and teardown are covered by
`tests/robot/test_registry.py` and `tests/robot/test_session.py`.

Telemetry ingestion landed with the simulator: `robot/telemetry.py` plus
`robot/session.py:handle_notification`, dispatched from the three-line hook in
`core/providers/tools/device_mcp/mcp_handler.py` that §4.2 budgeted, so device
`notifications/*` become world state and events instead of a log line.

**Still open:** the bridge plugin under **plugins/robot_bridge/** (nothing is registered with
the LLM yet, which is Phase 4), the weak-reference behaviour of the registry (state is held by
the store, not by a reference to the connection), and the compatibility regression test that
runs a full voice turn with the robot subsystem loaded.

**Key constraints** (robot-architecture §4.1, §4.2, R1–R5):

* All robot state on `conn` goes under **one** namespaced attribute. `ConnectionHandler`
  has no fixed shape and a long tail of attributes grafted on by other modules.
* The bridge **self-asserts** after registration. `plugins/__init__.py:_import_module_safe`
  catches every import exception and logs a warning, so a typo otherwise disables the whole
  robot subsystem while the server looks healthy.
* The bridge must not re-export any `BasePlugin` subclass:
  `plugins/__init__.py:register_plugins_to_conn` reflects over `dir(module)` with no dedup,
  so a re-exported class is instantiated once per module that names it.
* Use `if TYPE_CHECKING:` for `ConnectionHandler`, the way the inherited executors do
  (`core/providers/tools/server_plugins/plugin_executor.py`) — the class is not importable
  at plugin import time without a cycle.
* Never submit to `conn.executor` (a shared 5-worker pool), never assume `conn.tts` /
  `conn.asr` / `conn.func_handler` are non-`None`, and never cache `conn.logger` — it is
  replaced once the module string is known
  (`core/connection.py:ConnectionHandler._initialize_components`).
* The registry holds weak references and tolerates double-detach. `ConnectionHandler.close`
  is reachable from several call sites with no idempotency flag, and double teardown is the
  normal flow (`core/connection.py:ConnectionHandler.handle_connection`, `_save_and_close`).

### Acceptance criteria

* **Done.** Connecting two simulated robots yields exactly two registry entries with
  distinct device ids; disconnecting one leaves exactly one; disconnecting the same session
  twice is a no-op.
* **Done.** A test kills a simulated device without a clean close (drop the TCP connection)
  and the registry entry is gone within a bounded time.
* `git diff --stat` against the integration branch shows **at most 6 added lines** in
  `core/`, in two files: 3 in `core/connection.py` (an import plus attach and detach) and 3 in
  `core/providers/tools/device_mcp/mcp_handler.py` (the telemetry hook, which §4.2 budgeted at
  4). The whole budget is about 10 lines across 4 files.
* The bridge's self-assertion fails the server start (loudly, with a non-zero exit or a
  startup error) when a robot module is deliberately broken. Tested.
* A normal, non-robot client still completes a full voice turn with the bridge loaded — the
  compatibility regression test, run in CI from Phase 2 onward. See [testing.md](testing.md).

---

## Phase 3 — Action system and safety policy

The core of the design rule. No LLM involved yet; actions are driven from tests.

**Build:** **robot/actions/** (model, lifecycle, queue, resource claims), **robot/safety/**
(policy, limits, watchdog, e-stop). The intended design is written up under "Planned" in
[safety.md](safety.md); this phase implements it.

**Key constraints** (robot-architecture §2.8, §2.9, §3.1, R6, R7):

* Lifecycle is
  `PENDING → STARTING → RUNNING → SUCCEEDED | FAILED | CANCELLED | TIMED_OUT | REJECTED`.
  Illegal transitions raise.
* Resource claims over `DRIVE / HEAD / LIFT / DISPLAY / AUDIO / CAMERA`. Two actions
  claiming the same resource cannot run concurrently.
* Every semantic request is clamped against limits and sensor state **before** dispatch.
  The safety layer is a policy filter, not a guarantee — it does not replace firmware.
* Every dispatch to the device passes an **explicit short timeout** to `call_mcp_tool`. Its
  default is 30 seconds and the device executor never overrides it
  (`core/providers/tools/device_mcp/mcp_handler.py:call_mcp_tool`,
  `core/providers/tools/device_mcp/mcp_executor.py:DeviceMCPExecutor.execute`) — two orders
  of magnitude too long for a motion acknowledgement.
* The watchdog runs on the **robot's own loop or thread**, never the session event loop,
  which runs ONNX VAD inference (`core/providers/vad/silero.py`) and blocking HTTP. The
  periodic GC pass is dispatched to an executor but still walks `gc.get_objects()` twice
  and calls `gc.collect()` under the GIL every 300 seconds
  (`core/utils/gc_manager.py:GlobalGCManager._run_gc`, started from `app.py`), which stalls every
  Python thread in the process.
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
* E-stop: every queued action is `CANCELLED` and no new action is accepted until explicitly
  cleared.
* A test asserts that the safety package imports nothing from the behaviour or personality
  packages — enforced by the Phase 0.1 lint rule, and asserted again here.
* [safety.md](safety.md) is updated in the same change: which protections are duplicated in
  firmware, which exist only in the backend, and the failure mode of each when the Python
  process dies. Backend-only protections are, by definition, not guarantees.

---

## Phase 4 — Semantic action vocabulary (the LLM seam)

Expose actions to the LLM. This is where the critical design rule becomes real.

**Build:** the tool module inside **plugins/robot_bridge/**.

**Key constraints** (robot-architecture §4.1 seam ③, §3.3, R8, R9; see also [mcp.md](mcp.md)):

* `@register_function(name, schema, ToolType.IOT_CTL)`. `IOT_CTL` (code 5) is the **only**
  type auto-exposed to the LLM without editing `config.yaml`
  (`core/providers/tools/server_plugins/plugin_executor.py:ServerPluginExecutor.get_tools`),
  and one of the two branches that pass `conn`; `async def` handlers are awaited
  (`ServerPluginExecutor.execute`). Import `ToolType` from `plugins/register.py`, **not**
  from `core/providers/tools/base/tool_types.py` — same name, different enum.
* Every tool is namespaced `robot_*`. The tool namespace is flat, and executors are
  registered server-plugins-first, device-MCP-later
  (`core/providers/tools/unified_tool_handler.py`), so a device tool of the same name
  overwrites the guarded one in `core/providers/tools/unified_tool_manager.py:ToolManager.get_all_tools`
  with nothing worse than a logged warning.
* Every handler validates, clamps through the safety layer, enqueues, and **returns
  immediately** — `Action.NONE` for silent execution, `Action.RECORD` to log into history
  without a second LLM round-trip. Never block on hardware: tool futures are awaited
  sequentially, each bounded by `tool_call_timeout` (default 30 s), with no cancellation
  (`core/connection.py`, the `futures_with_data` loop in `chat`).
* Deployments select the function-call intent module (`selected_module.Intent: function_call`,
  whose `Intent.function_call.type` is `function_call`). The intent LLM provider renders its tool
  prompt once and caches it on the instance
  (`core/providers/intent/intent_llm/intent_llm.py:IntentProvider.detect_intent`, the
  `self.promot` guard), so a second robot with different hardware can be offered the first
  robot's tools.
* Vocabulary is integers with the unit in the parameter name (`distance_mm`, `angle_deg`,
  `speed_mmps`), allowed string values enumerated in the description, defaults on
  everything optional. The server imposes no argument-type restriction of its own — it
  forwards the device's `properties` object to the LLM unchanged ([mcp.md](mcp.md)) — so the
  narrow boolean/integer/string vocabulary is a firmware property the robot tools have to
  respect rather than something the server will catch.

### Acceptance criteria

* `robot_move`, `robot_turn`, `robot_look_at`, `robot_play_animation`, `robot_follow` and
  `robot_stop` appear in `func_handler.get_functions()` on a fresh connection **with no
  `config.yaml` edit**. Asserted by a test that builds a
  `core/providers/tools/unified_tool_handler.py` handler against a fake conn.
* A test enumerates every registered robot tool schema and asserts that no parameter name
  or description matches a raw-actuator denylist (`pwm`, `duty`, `servo_us`, `wheel_speed`,
  `voltage`, …). This is the mechanical form of the design rule.
* A test asserts that no robot tool schema contains a `number` type — nothing in the server
  rejects one and the firmware vocabulary cannot express it, so a float parameter is a latent
  bug.
* Every tool handler returns in **under 10 ms** with the motion layer stalled. Measured,
  not assumed.
* A collision test: when the simulated device advertises a tool named `robot_move`, the
  bridge detects the collision at session start and refuses, loudly, rather than letting
  the device tool shadow the guarded one.
* The simulator's robot tool set is advertised across as many `tools/list` pages as the
  device-side page budget requires, and the server assembles all of them
  (`core/providers/tools/device_mcp/mcp_handler.py:send_mcp_tools_list_continue_request`).
* An end-to-end test with a scripted fake LLM: utterance in → tool call → clamped action →
  simulator executes → completion event → world state updated. No real LLM, no network.

---

## Phase 5 — Vision and world model

**Build:** **robot/vision/**, extend **robot/state/**.

**Key constraints** (robot-architecture §2.4, R11):

* Coordinates are normalized 0.0–1.0 so tracking is resolution-independent.
* Do **not** route robot perception through `core/api/vision_handler.py`: it deep-copies
  the config and builds a VLLM provider per request, and runs the model call inside the
  aiohttp handler. Call `core/utils/vllm.py:create_instance` once from robot code and run
  `response()` in an executor.
* Copy the short-circuit convention in
  `core/providers/tools/device_mcp/mcp_executor.py:DeviceMCPExecutor.execute` for
  low-latency perception replies: a JSON result carrying an `action` field is turned
  straight into an `ActionResponse` and skips the second LLM pass.
* The vision auth token is minted per connection and carries a fixed lifetime
  (`core/utils/auth.py:AuthToken.generate_token`, one hour, never refreshed), so a session
  older than an hour holds an expired token. Either refresh it or document the ceiling.

### Acceptance criteria

* Tracking tests run against recorded frames in a committed fixtures directory under
  `tests/robot/`; no camera, no cloud, no network in CI.
* A target persists across frames with a stable id and decays out of the world model on a
  documented schedule; a test proves the decay.
* Vision never blocks the session event loop: a test asserts a voice turn completes within
  budget while a perception request is in flight.
* A session older than the token lifetime either still works (refresh implemented) or fails
  with a typed, logged error — not a bare 401 swallowed somewhere.

---

## Phase 6 — Behaviour engine and personality

**Build:** **robot/behavior/**, **robot/personality/**, **robot/animation/**.

**Key constraints** (robot-architecture §2.6, §7):

* Utility scoring must be **deterministic** given the same world state — seeded, no wall
  clock, no ambient randomness. Behaviour tests that flake are worse than no tests.
* The engine runs with **no LLM available**. This is a hard requirement, not a fallback.
* Animations are data-driven YAML; adding one requires no Python change.
* Personality influences scoring. It cannot influence safety. Enforced by layering.
* Emotion is an internal control signal. The inherited emoji-scrape
  (`core/utils/textUtils.py:get_emotion`) picks the first emoji in the reply, defaults to
  `happy`, and is emitted at most once per turn — treat it as one weak input, never the
  source of truth.

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

**Build:** **robot/api/**, **robot/memory/**. Spend the 3-line `app.py` budget.

**Key constraints** (robot-architecture §4.2, §4.3, R5, R11, R12):

* Own aiohttp app on its own port. Do **not** add routes to
  `core/http_server.py:SimpleHttpServer._build_app`: that table is built from the protocol
  registry and serves device traffic, and every route added there is an inherited-file edit
  plus an admin surface on the device port.
* The robot task gets a done-callback that escalates into the safety layer. `app.py` creates
  the WebSocket and HTTP tasks and never inspects them, so a port conflict degrades the
  server silently.
* Robot admin credentials are **distinct** from the device-token signing key. The OTA
  endpoint is unauthenticated and, when auth is enabled, mints a valid token for whatever
  device id the caller asks for (`core/api/ota_handler.py:OTAHandler.handle_post`,
  `core/auth.py:AuthManager`), so "the caller holds a valid device token" never authorizes
  actuation. See [safety.md](safety.md).
* Construct TTS/LLM/memory **per device**; never call
  `core/utils/modules_initialize.py:initialize_modules` with `init_tts=True` from robot
  code. Sharing one TTS pipeline between two robots shares its queues, its threads and its
  `close()`.
* Robot persistence is its own locked, durable store with a real shutdown await — not the
  pattern in `core/providers/memory/mem_local_short/mem_local_short.py`, which
  read-modify-writes one shared YAML file.
* Do not reuse `config/manage_api_client.py:ManageApiClient`. It is a process-wide
  singleton, and after `safe_close()` every later call raises rather than reconnecting.

### Acceptance criteria

* The API enumerates connected robots, reports per-robot state, accepts a semantic action,
  and accepts an e-stop. Every endpoint has a test.
* An unauthenticated request to every mutating endpoint is rejected. A valid **device**
  token is rejected for admin endpoints.
* Two simulated robots run a full session concurrently with **no** cross-talk: separate TTS
  pipelines, separate memory ids, separate world state. Asserted, because R5 says the naive
  path fails exactly here.
* Killing the robot API task causes a logged escalation and a safe state, not a silently
  degraded server.
* Robot state survives a server restart; a test writes, restarts the store, and reads back.

---

## Phase 8 — Hardware bring-up

Out of CI scope by definition. Tracked here so the boundary is explicit. The bring-up
procedure is written as part of this phase — there is no bring-up page today, and writing
one before the safety layer exists would document intentions as instructions.

* Ship a bring-up document with this phase: the firmware tool vocabulary, the calibration
  procedure, and the safety checklist to run before the first powered motion test. Add it
  to `scripts/check_docs.py` coverage like every other page.
* Calibration knobs are **config, not constants**. The inherited audio path already shows
  the pattern and the hazard: `AUDIO_FRAME_DURATION` and `PRE_BUFFER_COUNT` in
  `core/handle/sendAudioHandle.py` are module constants tuned for one class of device.
  Wheel diameter, gear ratio, servo trims and sensor offsets all drift on real hardware; a
  model that cannot be tuned is wrong on the bench.
* First powered test is on blocks, wheels off the ground, with a physical power cutoff
  within reach.

### Acceptance criteria

* Every firmware-side protection listed in [safety.md](safety.md) is demonstrated on
  hardware and the result recorded: cliff, collision, acceleration limit, watchdog timeout
  on link loss, e-stop.
* Link loss during motion halts the robot with the backend process **killed with
  `SIGKILL`** — proving the guarantee does not depend on Python cleanup.

---

## Ordering and what can run in parallel

```mermaid
graph LR
    P0["0 · Foundation"] --> P1["1 · Skeleton, bus, simulator"]
    P1 --> P2["2 · Registry, bridge"]
    P1 --> P5["5 · Vision, world model"]
    P2 --> P3["3 · Actions, safety"]
    P3 --> P4["4 · LLM tool vocabulary"]
    P4 --> P6["6 · Behaviour, personality"]
    P5 --> P6
    P3 --> P7["7 · API, memory, multi-robot"]
    P4 --> P7
    P6 --> P8["8 · Hardware bring-up"]
    P7 --> P8
```

Phase 5 needs only Phase 1's world model — **robot/vision** imports protocol, events and
state and nothing else (robot-architecture §7) — so it can run alongside Phases 2–4. Phase 6
needs both 4 and 5. Phase 7 needs 3 for e-stop and 4 for the action vocabulary.

## Risks carried forward

| Risk | Phase it bites | Current mitigation |
|---|---|---|
| Robot firmware may not speak the device MCP dialect the action layer assumes | 2, 4 | The protocol registry already supports adding a named protocol with its own routes (`robot/protocol/base.py:ProtocolSpec`). A robot-specific protocol is a new spec plus a route, not a fork of the session server. With `protocols.strict` shipped `true`, a device on an unregistered path is refused with 404 rather than quietly accepted, so the spec has to land before the firmware can connect. |
| Inherited code restructures `plugins/` or the tool registry on the next upstream sync | 2 | Bridge self-assertion fails loudly rather than silently; the compatibility regression test catches it in CI. See [upstream.md](upstream.md). |
| Device tools silently shadow guarded robot tools in the flat namespace | 4 | Collision detection at session start (Phase 4), because `ToolManager.get_all_tools` only logs a warning. |
| Provider instance sharing between two robots (R5) | 7 | Per-device construction, asserted by the two-robot concurrency test. |
| `os._exit(0)` restart path fires mid-motion (`core/connection.py:ConnectionHandler.handle_restart`) (R6) | 3, 8 | Firmware watchdog. Proven in Phase 8 with `SIGKILL`. |
| Actions are disabled by default and stay disabled until Phases 3–4 land; nothing in the tree can command motion today | ongoing | Unchanged by the migration, and deliberate. [safety.md](safety.md) states the boundary. |
| Upstream merge conflicts in the inherited files the robot work edits | ongoing | About 10 lines total, all `try/except`-wrapped, all documented in robot-architecture §4.2 with the rationale, so a conflict is resolvable without re-deriving the reasoning. |
