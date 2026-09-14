# Robot simulator

A complete simulated robot that speaks the Nilo device protocol, so the backend can be
developed and tested with no hardware.

It is a **client**, not a test double wired into the server. It opens the real WebSocket
route, sends a real `hello`, answers the device-MCP handshake the server starts, publishes
its hardware as MCP tools, and reports itself as JSON-RPC notifications. Everything the
backend learns about it, it learns over the socket. If a behaviour cannot be produced by a
device on a socket, the simulator cannot produce it either — which is what makes it usable
as the thing every robot test runs against ([robot-roadmap.md](robot-roadmap.md), "No
hardware in CI").

Code: `main/nilo-server/robot/simulator/`. Backend side of the telemetry channel:
`main/nilo-server/robot/telemetry.py`.

---

## 1. Quick start

Three terminals, or two plus a browser. All paths are relative to the repository root.

**1. Run the backend.**

```bash
cd main/nilo-server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
mkdir -p data && printf 'selected_module:\n  LLM: LMStudioLLM\n' > data/.config.yaml
python app.py
```

The log prints the routes. The simulator needs only the WebSocket one:

```
WebSocket endpoint [nilo]:	ws://<host>:8000/nilo/v1/
```

Full setup, including how to choose providers, is in
[getting-started.md](getting-started.md). If port 8000 is taken on your machine, start the
server with `NILO_SERVER_PORT=8100` and point the simulator at that port — a second process
bound to `127.0.0.1:8000` will silently win over a server bound to `0.0.0.0:8000`.

**2. Run the simulator.**

```bash
cd main/nilo-server
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --robot-id nilo-sim-01 --scenario person_enters_room
```

**3. Inspect the robot state.**

```bash
curl -s http://127.0.0.1:8090/state | python -m json.tool
```

or, equivalently, from any directory with the package importable:

```bash
python -m robot.simulator --status
```

The server's view of the same robot appears in its log:

```
robot nilo-sim-01 registered (device=nilo-sim-01 session=... reconnect=False)
robot nilo-sim-01: 16 tool(s) discovered (16 new, 0 malformed)
```

There is no management API yet — that is Phase 7 of [robot-roadmap.md](robot-roadmap.md) —
so the robot's own state endpoint plus the server log are the two windows into a running
session. The robot is authoritative and the server's copy is a cache
([robot-architecture.md](robot-architecture.md) Sect. 2.4), so the state endpoint is the
primary source, not a convenience.

---

## 2. What it speaks

| Step | Direction | Frame |
|---|---|---|
| Handshake | device → server | `hello` with `features: {mcp: true}` and Opus audio parameters |
| Welcome | server → device | `hello` carrying the `session_id` |
| MCP initialize | server → device | `{"type": "mcp", "payload": {"method": "initialize", ...}}`, including the vision endpoint URL and a device token |
| MCP initialize result | device → server | `serverInfo` naming the hardware model and firmware version |
| Tool discovery | server → device | `tools/list`, repeated with `nextCursor` while the device pages |
| Tool call | server → device | `tools/call` with the device's own (dotted) tool name |
| Telemetry | device → server | `notifications/*`, JSON-RPC with a method and no id |

The device id travels in the `device-id` header, which is what `--robot-id` sets. The
server normalizes it into a robot id (`aa:bb:cc` becomes `aa-bb-cc`), so the id you pass is
the id you see in the log.

Nothing in `robot/simulator/` imports `core/`, and `websockets` is imported lazily inside
the session, so the world model, the physics step and the scenario runner are importable
and testable with the dev-only dependency slice ([testing.md](testing.md)).

### Audio

The simulator has no microphone and no speaker. It sends no Opus frames and drops the
binary frames the server sends it, tracking only whether the server says it is speaking.
Voice is exercised by `scripts/smoke_check.py` and by a real device; the simulator exists
for the robot half of the protocol.

---

## 3. The tool table

Sixteen tools, published under dotted names. The server sanitizes each name for the LLM
function namespace (`robot.motion.move` becomes `robot_motion_move`) and calls back with
the original, which is exactly the round trip a real device exercises.

| Tool | Arguments | Returns |
|---|---|---|
| `robot.get_status` | — | The whole robot: pose, motion, battery, sensors, cliff, IMU, head, lift, expression, what it can see, and a summary of the room |
| `robot.motion.move` | `distance_mm` (-2000…2000, required), `speed_mmps` (20…400, default 200) | `action_id` immediately; completion arrives as a notification |
| `robot.motion.turn` | `angle_deg` (-360…360, required, positive turns left), `speed_dps` (10…180, default 90) | `action_id` immediately |
| `robot.motion.stop` | — | The `action_id` it cancelled |
| `robot.follow.target` | `target_id` (required), `duration_ms` (100…60000, default 5000), `stop_distance_mm` (100…3000, default 600) | `action_id` immediately; the device tracks the target itself and reports completion as a notification |
| `robot.head.set_angle` | `pitch_deg` (-25…40), `yaw_deg` (-90…90) | The angles after clamping |
| `robot.head.look_at` | `x_pct`, `y_pct` (0…100, required) | The head angles that aim at that spot in the camera frame |
| `robot.lift.set_position` | `height_pct` (0…100, required) | The lift height |
| `robot.expression.set` | `emotion` (required), `intensity_pct` (0…100, default 100) | The expression that is showing |
| `robot.animation.play` | `name` (required), `duration_ms` (100…30000, default 2000) | The animation that started |
| `robot.camera.capture` | `question` (optional) | A base64 PNG, what is in frame, and — with a question — the vision endpoint's answer |
| `robot.audio.set_volume` | `volume_pct` (0…100, required) | The volume |
| `robot.sensor.get_distance` | — | `front_mm`, `left_mm`, `right_mm`, the sensor range, and whether the bumper is pressed |
| `robot.sensor.get_imu` | — | Acceleration in milli-g, rotation in milli-degrees per second, orientation in degrees |
| `robot.sensor.get_cliff` | — | Per-sensor cliff booleans |
| `robot.power.get_battery` | — | Percentage, charging flag, cell millivolts, and where the dock is |

Every result is a JSON object in the MCP text content block, so a caller reading
`content[0].text` — which is what the inherited `call_mcp_tool` returns — gets structured
data. `robot.camera.capture` additionally attaches a proper MCP `image` content block.

### Why the arguments look like that

The table follows the vocabulary rules the action layer will be held to
([robot-roadmap.md](robot-roadmap.md) Phase 4), because the simulator's job is to be the
contract the firmware will have to meet:

* **Integers, bounded strings and booleans only.** Never a float: the server imposes no
  argument-type restriction of its own, and a firmware vocabulary that cannot express a
  float turns one into a latent bug.
* **The unit is in the name** — `distance_mm`, `angle_deg`, `speed_mmps`, `height_pct`.
* **Out-of-range values are clamped, not rejected.** That is what a servo with a mechanical
  stop does. Only a meaningless request (a zero-distance move, a nameless animation) is an
  error.
* **No parameter names a raw actuator.** No PWM, no duty cycle, no servo microseconds, no
  wheel speeds, no coil voltages. `tests/robot/test_simulator.py` asserts this over every
  published schema; it is the mechanical form of the rule that the LLM never controls
  motors ([robot-architecture.md](robot-architecture.md), [safety-model.md](safety-model.md)).

---

## 4. Telemetry

The simulator reports itself as MCP notifications — JSON-RPC with a `method` and no `id`,
so nothing is waiting for a reply:

```json
{"type": "mcp",
 "payload": {"jsonrpc": "2.0",
             "method": "notifications/battery",
             "params": {"percent": 41, "charging": false, "battery_mv": 3690}}}
```

| Method | When | Carries |
|---|---|---|
| `notifications/telemetry` | Every `--telemetry-ms` (default 1 s) | Every slice below, in one frame |
| `notifications/pose` | Every `--pose-ms` while moving | `x_mm`, `y_mm`, `yaw_deg`, `frame` |
| `notifications/battery` | When the whole-percent charge changes | `percent`, `charging`, `battery_mv` |
| `notifications/sensor` | When a cliff, bump or pickup flag changes | The flags plus the raw readings |
| `notifications/motion_completed` | A motion finished on its own | `action_id`, `kind`, and the pose it finished at |
| `notifications/motion_failed` | A motion ended without finishing | `action_id`, `kind`, `reason`, `detail`, and the sensors for a cliff or obstacle |

`reason` is one of `cancelled`, `obstacle`, `cliff`, `motor_failure`, `battery_empty`,
`superseded` or `target_lost`.

### The backend side

`robot/telemetry.py` parses these frames into `RobotTelemetry` and applies them to the
runtime; `robot/session.py:handle_notification` is the seam, and
`core/providers/tools/device_mcp/mcp_handler.py` dispatches into it from the branch that
otherwise logs the method name and drops the frame — the three-line inherited edit budgeted
in [robot-architecture.md](robot-architecture.md) Sect. 4.2. Without it the server's picture
of a robot could only ever be filled in by polling.

Conversion happens in exactly one place, in `robot/telemetry.py`: the wire vocabulary is the
device's (integer millimetres, degrees, percentages) and the world model stores metres,
radians and fractions. A malformed field is dropped rather than raised on, so a robot that
reports one bad number does not lose the rest of its frame.

Applying a frame publishes `TelemetryUpdated` plus the per-field `PoseUpdated`,
`BatteryUpdated` and `SensorUpdated` events already on the bus, and the new
`MotionCompleted` / `MotionFailed` events. Subscribe to those rather than polling the state
store ([robot-domain.md](robot-domain.md)).

---

## 5. The world, and what the sensors see

`robot/simulator/world.py` is a 2D room: walls (line segments), obstacles (circles), cliffs
(rectangles), people, objects, and a charging dock. It exists so that the readings a
behaviour test sees are consistent *with each other* — a front distance of 200 mm and a
completed 1 m move cannot both happen — not to be a physics engine.

| Sensor | How it is derived |
|---|---|
| Distance, front / left / right | Ray cast at 0°, +60°, -60° against every wall and obstacle, capped at the sensor range (2 m by default) |
| Cliff, front-left / front-right | Point test at two offsets ahead of the wheels |
| Bump | Front distance inside the bumper range |
| IMU | Derived from the current linear and angular speed, so it agrees with the pose |
| Battery | Drains while idle, faster while driving, charges on the dock |
| Camera | See below |

The default room is 4 m × 3 m with a dock at (200 mm, 200 mm); the robot starts at
(600 mm, 200 mm) facing +x, off the dock, so a scenario that is not about charging does not
begin in a charging state.

Noise is **off** by default so tests can assert exact readings. `--sensor-noise-mm` turns on
seeded Gaussian noise on the distance sensors; with `--seed` the run stays reproducible. Real
sensors never read zero-noise, so a run that is about robustness should use it.

---

## 6. Movement

Motion takes time. `move 1000 mm` at 200 mm/s occupies five seconds of simulated time,
during which the pose changes on every tick and the robot reports `moving`:

```
robot.motion.move  →  {"accepted": true, "action_id": "3f1c…", "state": "moving", "eta_ms": 5000}
                   →  notifications/pose      (every 250 ms while moving)
                   →  notifications/telemetry (every second)
                   →  notifications/motion_completed {"action_id": "3f1c…"}
```

A motion ends early, as `notifications/motion_failed`, when:

* `robot.motion.stop` is called — `reason: cancelled`
* a new motion command arrives — the old one is `superseded`
* the leading edge of the chassis reaches an obstacle or a wall — `obstacle`
* a cliff sensor asserts — `cliff`
* the motors are made to fail — `motor_failure`
* the battery reaches zero — `battery_empty`
* a follow's target leaves the world — `target_lost`

Collision is tested against the *leading edge* of the chassis, not its centre, so the robot
stops when its bumper reaches the obstacle.

### Following

`robot.follow.target` is the one motion that is closed-loop rather than open-loop, and it is
modelled that way on purpose: the backend names a target and a deadline, and the *device*
turns towards the bearing each tick, drives while it is further away than `stop_distance_mm`,
and holds station inside it. It terminates on its deadline, not on a distance, because the
target moves. Everything the backend can see is an `action_id` and, eventually, a
notification — which is exactly how much a real robot would tell it.

---

## 7. The camera

`robot.camera.capture` returns a PNG. Two sources:

* **Synthetic** (the default). `robot/simulator/camera.py` renders the field of view: a
  horizon, a floor gradient, and a box for each person, object and dock in frame, positioned
  by bearing and sized by distance. It is written by hand with `zlib` and `struct` rather than
  by an imaging dependency, because the only thing that has to be true of it is that the
  backend accepts it.
* **Fixtures.** `--camera-fixtures DIR` serves committed image files in a stable order,
  looping. For anything where a coloured box is not a useful answer.

The frame comes back base64-encoded in the JSON result *and* as an MCP `image` content block.
`tests/integration/test_simulator_e2e.py` decodes it and runs the server's own sniffer
(`core/utils/util.py:is_valid_image_file`, which is what the vision endpoint uses) over the
bytes.

With a `question` argument, the simulator also POSTs the frame to the vision endpoint the
server handed over during the MCP handshake — the same multipart upload a real device
performs against `core/api/vision_handler.py` — and returns the answer alongside the image.
A failure there degrades to a message in `vision_answer`; it never fails the capture.

Note that the URL in the handshake is generated from the server's detected LAN address
unless `server.vision_explain` is set, so on a loopback-only server set it explicitly
([configuration.md](configuration.md)).

---

## 8. The clock

Nothing in the simulator reads the wall clock directly; everything goes through
`robot/simulator/clock.py`.

| Clock | Used by | Behaviour |
|---|---|---|
| `RealClock(speed)` | The CLI, and the integration tests | Wall time multiplied by `--speed`. `--speed 20` runs a 60 s scenario in 3 s, and the physics step still sees a consistent `dt` |
| `ManualClock` | Unit tests | Time moves only on `advance()`. The same sequence of calls produces the same run on every machine |

Timing *faults* — packet delay, a tool that never answers — deliberately use real seconds
instead, because they exist to trip the server's real timeouts, which an accelerated clock
would otherwise scale away.

---

## 9. Scenarios

A scenario is data: a start pose, a room, a fault set, and a list of steps on a timeline.
`--list-scenarios` prints the built-ins.

| Scenario | What happens |
|---|---|
| `idle` | Nothing. Telemetry only — the baseline for a connection test |
| `person_enters_room` | A person appears in front of the robot, which reacts and then watches them come closer |
| `person_leaves_room` | A person who was there walks out; the robot is alone again |
| `robot_gets_bored` | Nobody around: idle expression, fidget animation, a look left and right |
| `battery_low` | Charge falls into the low band and keeps draining while the robot drives |
| `obstacle_during_move` | An obstacle appears in the path of a move already in flight, then is removed |
| `cliff_during_move` | The floor ends mid-move; the cliff sensors assert and the motion fails |
| `charger_found` | Low battery, the robot drives onto the dock and starts charging |

```bash
python -m robot.simulator --scenario cliff_during_move --speed 10 --duration 20
python -m robot.simulator --scenario battery_low --print-scenario   # resolved scenario as JSON
```

### Writing your own

`--scenario-file PATH` loads YAML (or JSON) with the same shape:

```yaml
description: A person walks past while the robot is driving
duration_s: 30
start_x_mm: 600
start_y_mm: 200
start_yaw_deg: 0
start_battery_pct: 60
faults:
  packet_delay_ms: 0
steps:
  - at_s: 2
    do: move
    args: {distance_mm: 1500, speed_mmps: 200}
  - at_s: 4
    do: spawn_person
    args: {id: visitor, x_mm: 1800, y_mm: 600}
  - at_s: 6
    do: set_expression
    args: {emotion: curious, intensity_pct: 70}
  - at_s: 9
    do: fault
    args: {camera_failure: true}
```

`at_s` is simulated seconds **from the moment the server finishes discovering the robot**,
not from the start of the process. A scripted step on an accelerated clock would otherwise
land inside the WebSocket handshake — on an eight-times clock, `at_s: 2` arrives a quarter
of a second in — and fire into a socket nobody is listening on, which makes a run depend on
how busy the host is. A reconnect does not restart the timeline: a scenario is a story, not
a loop. A simulator with no server (a test driving `step()` by hand) runs its timeline from
zero, exactly as simulated time does, and `status()["scenario"]["started"]` says which of
the two a run is in.

Steps run in time order, once each, driven from the same tick loop as the physics, which is
why an accelerated or manual clock works. An unknown or failing step is logged and skipped,
never fatal.

Steps, and the arguments they take (distances in millimetres, angles in degrees):

| `do:` | `args:` |
|---|---|
| `spawn_person` / `remove_person` | `id`, `x_mm`, `y_mm`, `name` |
| `add_object` | `id`, `x_mm`, `y_mm`, `label` |
| `add_obstacle` / `remove_obstacle` | `id`, `x_mm`, `y_mm`, `radius_mm` |
| `add_cliff` / `remove_cliff` | `id`, `x0_mm`, `y0_mm`, `x1_mm`, `y1_mm` |
| `set_battery` | `percent` |
| `move` | `distance_mm`, `speed_mmps` |
| `turn` | `angle_deg`, `speed_dps` |
| `stop` | — |
| `follow` | `target_id`, `duration_ms`, `stop_distance_mm` |
| `set_expression` | `emotion`, `intensity_pct` |
| `play_animation` | `name`, `duration_s` |
| `set_picked_up` | `picked_up` |
| `fault` | Any field of the fault set (see below) |
| `disconnect` | `reconnect` |
| `log` | `message` |

---

## 10. Failure injection

Every fault is off by default, settable from the CLI, from a scenario's `faults:` block, and
from a `fault` step mid-run.

| Flag | `faults:` key | Effect |
|---|---|---|
| `--packet-delay-ms N` | `packet_delay_ms` | Delays every frame the simulator sends, in real milliseconds |
| `--tool-timeout TOOL` | `tool_timeout` | That tool accepts the call and never answers, so the server's timeout fires. Repeatable |
| `--tool-error TOOL` | `tool_error` | That tool answers with an MCP `isError` result. Repeatable |
| `--motor-failure` | `motor_failure` | Any motion in flight ends as `motor_failure` |
| `--camera-failure` | `camera_failure` | `robot.camera.capture` fails instead of returning a frame |
| `--drop-notifications` | `drop_notifications` | Telemetry stops; the session stays up and looks healthy |
| `--drop-motion-completion` | `drop_motion_completion` | Telemetry keeps flowing and the motion really happens, but its `motion_completed` / `motion_failed` notification is never sent. The narrower version of the fault above, and the only way to exercise the backend action watchdog on a live, healthy link |
| `--disconnect-at SEC` | `disconnect_at_s` | Aborts the TCP connection with no close frame, the way a robot losing power does |
| `--no-reconnect` | `reconnect: false` | Exit instead of redialling a dropped link |

A low battery is a scenario step (`set_battery`) or the `--battery` flag, not a fault: it is
a legitimate state, not a malfunction. Obstacles and cliffs appearing are likewise world
changes, added with `--obstacle` / `--cliff` or a scenario step.

```bash
# The server's tool timeout, exercised end to end
python -m robot.simulator --tool-timeout robot.get_status

# A healthy-looking robot whose motions never report back: the action watchdog's job
python -m robot.simulator --drop-motion-completion

# A robot that goes away mid-scenario and comes back
python -m robot.simulator --scenario obstacle_during_move --disconnect-at 6 --reconnect-delay 1
```

---

## 11. Inspecting state

**The robot's own state** — the authoritative copy — is served by the simulator on
`--status-port` (default 8090, `0` disables it):

```bash
curl -s http://127.0.0.1:8090/state | python -m json.tool
python -m robot.simulator --status                    # the same thing, formatted
python -m robot.simulator --status --status-port 8091 # a second simulator
```

It returns the connection, the simulated time and clock speed, the scenario and every step
that has run, the live fault set, the published tools, the last twenty tool calls, how many
notifications have been sent, and the full robot snapshot (pose, motion, battery, sensors,
cliff, IMU, head, lift, expression, what is in frame, and a summary of the room).

**The server's cached copy** is reachable from Python — there is no HTTP admin surface until
Phase 7:

```python
from robot.runtime import get_runtime

state = await get_runtime().registry.get("nilo-sim-01")
print(state.telemetry.battery, state.telemetry.pose, state.capabilities.tool_names)
```

and in the server log, which reports registration, discovery, telemetry changes and
disconnects at `INFO`. Run the server with `NILO_LOG_LEVEL=DEBUG` to see every frame.

---

## 12. Command line reference

```
python -m robot.simulator [options]
```

| Group | Options |
|---|---|
| Connection | `--server URL`, `--robot-id ID`, `--client-id ID`, `--token TOKEN`, `--no-reconnect`, `--reconnect-delay SEC` |
| Run | `--scenario NAME`, `--scenario-file PATH`, `--list-scenarios`, `--print-scenario`, `--speed X`, `--duration SEC`, `--tick-ms N`, `--telemetry-ms N`, `--seed N`, `--tools-page-size N`, `--log-level LEVEL` |
| Hardware | `--max-speed-mmps N`, `--max-turn-dps N`, `--sensor-noise-mm X`, `--battery PCT`, `--camera-fixtures DIR` |
| World | `--room W_MM,D_MM`, `--obstacle X_MM,Y_MM,R_MM`, `--cliff X0,Y0,X1,Y1`, `--person X_MM,Y_MM`, `--no-dock` |
| Failure injection | `--packet-delay-ms N`, `--tool-timeout TOOL`, `--tool-error TOOL`, `--motor-failure`, `--camera-failure`, `--drop-notifications`, `--disconnect-at SEC` |
| State | `--status-port N`, `--status-host HOST`, `--status` |

`--token` is only needed when `server.auth.enabled` is true and the robot id is not on the
allow-list; the OTA endpoint mints one ([protocol.md](protocol.md)).

`--tools-page-size N` makes the device publish its tools across several `tools/list` pages,
which is how the server's cursor handling gets exercised.

---

## 13. Tests

| File | Needs | Covers |
|---|---|---|
| `main/nilo-server/tests/robot/test_simulator.py` | Dev slice only | Clock, world, sensors, the physics step, motion outcomes, the camera, every tool schema and handler, the scenario runner, and a deterministic replay. No socket |
| `main/nilo-server/tests/robot/test_telemetry.py` | Dev slice only | Notification parsing, unit conversion, malformed fields, motion events, and ingestion into a runtime |
| `main/nilo-server/tests/integration/test_simulator_e2e.py` | `websockets`, `opuslib_next`, `numpy` | The simulator against a server started in-process: connection, two robots, capability discovery (including paged), telemetry reaching the world state, tool calls, the camera image, movement lifecycle, cancellation, obstacle and cliff, injected failures, abrupt disconnect and reconnect |

```bash
cd main/nilo-server
pytest -q tests/robot/test_simulator.py tests/robot/test_telemetry.py   # no socket, fast
pytest -q tests/integration                                            # the end-to-end suite
```

The integration fixture starts the real `core/websocket_server.py` with one thing stubbed:
`initialize_modules` returns nothing, so no VAD, ASR, LLM, memory or intent provider is
constructed. Everything else is the production path — the real connection handler, the real
`hello` handler, the real device MCP handshake and the real robot seam. What it skips is a
model download, which no robot test needs.

Two details in that fixture are load-bearing and easy to get wrong again:

* The config cache entry must be **repopulated** after it is dropped.
  `config/logger.py:setup_logging` reads it from inside the running loop and falls back to
  `asyncio.run()` when it is missing, which cannot work there — the hazard
  [robot-architecture.md](robot-architecture.md) Sect. 4.3 warns about.
* Each test installs its own runtime with `robot.runtime.set_runtime`, so two tests never
  share a registry.

---

## 14. What it does not simulate

Stated plainly, because a simulator's limits are the limits of every test that uses it:

* **No audio.** No microphone, no Opus, no VAD, no ASR, no speaker. The voice pipeline is
  exercised by a real device and by `scripts/smoke_check.py`.
* **No real dynamics.** No acceleration ramp, no wheel slip, no mass, no motor current.
  Speed is reached instantly and held exactly. The real firmware owns acceleration limits
  ([safety-model.md](safety-model.md)); a backend test should not depend on the shape of that ramp.
* **No localization error.** The pose is exact by construction. Real odometry drifts, and a
  behaviour that only works with a perfect pose will not work on hardware.
* **2D only.** No pitch, no roll, no ramps, no stairs beyond "the floor is not there".
* **The camera is a diagram, not a photograph.** Use `--camera-fixtures` for anything a
  vision model has to answer meaningfully about.

It is, deliberately, enough to test everything the backend does: the protocol, discovery,
the action lifecycle, telemetry, world state, failure handling and reconnection.
