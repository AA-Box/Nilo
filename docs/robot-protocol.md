# The robot device protocol

What a device has to put on the wire to be a robot, and what the server does with each
frame. One page, because "is my firmware correct?" should not be a tour of six.

This is the *robot* half. The session protocol underneath it — the WebSocket route, the
`hello` handshake, audio framing, OTA — is [protocol.md](protocol.md), and the device-MCP
mechanics are [mcp.md](mcp.md). Nothing here replaces those; it names the subset a robot
adds and the exact shapes the backend parses.

The reference implementation on the device side is
[`robot/simulator/`](../main/nilo-server/robot/simulator/), which speaks every frame below
over a real socket, and the firmware is
[Nilo-esp32](https://github.com/AA-Box/Nilo-esp32).

## 1. The shape of a session

```
device                                        server
  |  WebSocket connect  /nilo/v1/               |
  |  headers: device-id, client-id, [auth]      |
  |-------------------------------------------->|
  |                                    hello    |  session_id issued
  |<--------------------------------------------|
  |  hello {features: {mcp: true}, audio_params}|
  |-------------------------------------------->|  robot registered
  |                                             |
  |<-- mcp initialize --------------------------|  capability discovery
  |--- mcp initialize result ------------------>|   (off the read loop)
  |<-- mcp tools/list --------------------------|
  |--- mcp tools/list result (paged) ---------->|  capabilities recorded
  |                                             |
  |--- mcp notifications/telemetry ------------>|  world state
  |--- mcp notifications/pose ----------------->|
  |                                             |
  |<-- mcp tools/call robot.motion.move --------|  one action
  |--- mcp result {accepted, action_id} ------->|
  |--- mcp notifications/motion_completed ----->|  the action settles
```

Three properties are load-bearing:

* **Discovery runs off the read loop.** The inherited read loop awaits every message
  handler inline, so anything that waits on a device would stall ingestion of all later
  frames, audio included. The server therefore returns from "connected" immediately and
  discovers in a background task (`robot/runtime.py`).
* **A reconnect rediscovers.** Firmware may have changed between sessions, so capabilities
  are dropped on disconnect and rebuilt on the new session — never carried over.
* **The device is authoritative about itself.** The server never infers that a motion
  finished. It knows because the device said so.

## 2. Identity

The robot id is derived from the `device-id` header, normalized: lowercased, and every
character outside `[a-z0-9]` collapsed to `-`. A device that connects as
`AA:BB:CC:00:00:01` is `aa-bb-cc-00-00-01` in every API path, event and log line.

That is the whole identity scheme. There is no registration step and no second id.

## 3. Tools: what the robot can be asked to do

A robot publishes its hardware as MCP tools over the session's device-MCP channel. Names
are dotted on the wire and sanitized by the server (`.` becomes `_`), so
`robot.motion.move` is `robot_motion_move` in every capability lookup, API response and
metric label. The server remembers the original and calls back with it.

The sixteen tools the simulator publishes, and which the firmware implements today:

| Tool | Firmware | What it does |
| :--- | :--- | :--- |
| `robot.get_status` | yes | Everything the robot knows about itself, in one reply |
| `robot.motion.move` | yes | Drive straight, ±2000 mm. Returns an `action_id` |
| `robot.motion.turn` | yes | Turn in place, ±360°. Returns an `action_id` |
| `robot.motion.stop` | yes | Cancel the motion in flight |
| `robot.head.look_at` | yes | Point the head at a spot in the frame, in percent |
| `robot.head.set_angle` | yes | Absolute head pitch and yaw, clamped not rejected |
| `robot.lift.set_position` | yes | Raise or lower the lift, 0–100 % |
| `robot.expression.set` | yes | Show an emotion on the face |
| `robot.animation.play` | yes | Play a named animation |
| `robot.camera.capture` | yes | One frame, base64, as PNG or JPEG |
| `robot.power.get_battery` | yes | Battery state, and the dock when one is known |
| `robot.sensor.get_distance` | yes | Forward-facing range sensors |
| `robot.sensor.get_imu` | yes | Acceleration, rotation rate, orientation |
| `robot.sensor.get_cliff` | yes | Downward-facing cliff sensors |
| `robot.follow.target` | **no** | Follow a tracked target for a bounded time |
| `robot.audio.set_volume` | **no** | Set the speaker volume |

The last two are published by the simulator and not yet by the firmware. That gap is real
and is recorded in [`PROJECT_STATUS.md`](../PROJECT_STATUS.md); a device is free to publish
fewer tools, and the server refuses to dispatch anything a device did not publish before
the call leaves the process.

**Publishing a tool is the whole permission model at this layer.** A tool a device does not
publish cannot be called by an operator, a behaviour or a model; a tool it does publish can
still be refused by the safety policy ([safety-model.md](safety-model.md)).

### A motion tool's reply

Motion tools return *immediately*. The reply says whether the command was accepted and
gives the id the completion will refer to:

```json
{"accepted": true, "state": "moving", "action_id": "dev-17"}
```

A device that refuses says so in the same shape, with a reason:

```json
{"accepted": false, "state": "idle", "reason": "cliff"}
```

Everything else returns its data directly. A tool that fails returns an MCP tool error;
the server turns that into a failed action rather than a hung one.

## 4. Telemetry: what the robot says about itself

Telemetry is a JSON-RPC **notification** — a `method` with no `id`, so nothing is waiting
for a reply — on the same MCP channel:

```json
{"type": "mcp", "payload": {"jsonrpc": "2.0",
                            "method": "notifications/telemetry",
                            "params": { "...": "..." }}}
```

Eight slice methods carry one block each; `notifications/telemetry` carries any subset of
them at once:

| Method | `params` |
| :--- | :--- |
| `notifications/pose` | `{"x_mm": 600, "y_mm": 200, "yaw_deg": 0, "frame": "odom"}` |
| `notifications/motion` | `{"moving": false, "action_id": null, "linear_speed_mmps": 0, "angular_speed_mdps": 0}` |
| `notifications/battery` | `{"percent": 85, "charging": false, "battery_mv": 4065}` |
| `notifications/sensor` | `{"cliff_detected": false, "bump_detected": false, "picked_up": false, "touch_detected": false, "readings": {"front_mm": 2000, ...}}` |
| `notifications/audio` | `{"listening": false, "speaking": false, "volume_pct": 50}` |
| `notifications/vision` | `{"camera_active": true, "faces_detected": 0}` |
| `notifications/expression` | `{"emotion": "neutral", "intensity_pct": 0, "animation": null}` |
| `notifications/activity` | `{"activity": "idle", "detail": null}` |
| `notifications/telemetry` | a mapping of the slice names above to their blocks |

**Units are the device's, and they are integers**: millimetres, degrees, percent,
millivolts, millidegrees per second. Conversion to the metres and radians the world model
stores happens in exactly one place (`robot/telemetry.py`). A malformed field is dropped
and the rest of the frame is kept: a robot that reports one bad number must not lose the
frame it was in.

### How often

There is no required rate, and the server does not poll. What there *is* is a freshness
requirement in the other direction: the safety policy refuses to start a motion when the
sensor picture is older than `max_sensor_age_s` (2 s by default), and the heartbeat check
refuses when nothing has been heard for `heartbeat_timeout_s` (5 s). A device that reports
once a second is comfortable; one that reports every ten seconds cannot be driven.

Telemetry that repeats the same numbers is cheap: the server compares values, ignoring
timestamps, and publishes nothing when nothing changed.

## 5. Completion: how a motion ends

Every motion the device accepted ends with exactly one notification:

```json
{"method": "notifications/motion_completed", "params": {"action_id": "dev-17", "kind": "move"}}
{"method": "notifications/motion_failed",    "params": {"action_id": "dev-17", "kind": "move",
                                                        "reason": "cliff",
                                                        "detail": "cliff sensor front-left asserted"}}
```

`reason` is the device's own word for what happened. The vocabulary the simulator and the
firmware share: `cancelled`, `obstacle`, `cliff`, `bump`, `motor_failure`, `battery_empty`,
`superseded`, `target_lost`, `link_lost`.

Four rules the backend implements, and firmware should be written against:

1. **A completion is matched on the device's `action_id`**, never on "whatever this robot
   is doing". A completion for an id the server does not recognize is logged and dropped.
2. **A duplicate completion settles nothing twice.** Firmware that retries a notification,
   or a reconnect that replays one, is safe.
3. **A completion that never arrives is not a hang.** The action executor arms a watchdog
   at dispatch; an unsettled action becomes `TIMED_OUT` on its own.
4. **A new motion supersedes the old one.** A device that accepts a move while already
   moving must report the first one ended, with `reason: "superseded"`.

## 6. Speech and listening

A robot that has a microphone reports the recognizer's *output*, not audio, exactly as any
device on this session protocol does:

```json
{"type": "listen", "mode": "manual", "state": "detect", "text": "come a little closer"}
```

and stops the robot talking when the person talks over it:

```json
{"type": "abort"}
```

The server sends `{"type": "tts", "state": "start" | "stop" | "sentence_start"}` frames
around synthesized speech, followed by the audio itself as binary frames. A device with no
speaker may drop the audio and still use the state frames to drive its face.

## 7. What the device owns, and the backend cannot

The backend's safety policy is a filter in front of the action layer. It is not a
guarantee, and it cannot be one: the frame that would stop a robot travels on a network
that has just failed. Five protections therefore belong to the device, and the firmware
implements them with no way for the backend to relax any of them
([safety-model.md](safety-model.md), and `board/safety_supervisor.h` in the firmware):

| The device stops itself when | Why the backend cannot |
| :--- | :--- |
| a cliff sensor asserts | the reading is 20 ms old and the stop is a round trip away |
| something is inside the clearance | same |
| it is picked up | same |
| **the link died mid-motion** | there is no link to send a stop on |
| the command heartbeat expired | the backend is the thing that stopped talking |

The link rule has a grace period (1 s in the firmware, and in the simulator). Scenario J of
the end-to-end suite asserts on it: the socket dies mid-move, and the *device* stops.

## 8. Faults a correct device may produce, and the server's answer

| The device does | The server does |
| :--- | :--- |
| never answers a tool call | times the call out and fails the action; the link stays up |
| answers a tool call with an error | the action fails with the device's message |
| sends a notification with no `params` | drops it; the session continues |
| sends malformed JSON | drops the frame; the session continues |
| sends a response to an id nobody asked about | drops it |
| drops the socket with no close frame | marks the robot disconnected, cancels its actions |
| reconnects with the same id | a new session; capabilities rediscovered from scratch |
| publishes a tool with a malformed schema | counts it in `malformed_tools` and keeps the rest |

Every row is a test in `tests/e2e/test_robustness.py` or `tests/integration/`.

## Related pages

* [protocol.md](protocol.md) — the session protocol this sits on
* [mcp.md](mcp.md) — the device-MCP channel and the tool registry
* [robot-actions.md](robot-actions.md) — what the server does with an accepted action
* [safety-model.md](safety-model.md) — the split between what the device owns and what the backend filters
* [robot-simulator.md](robot-simulator.md) — a device that speaks all of this, over a real socket
