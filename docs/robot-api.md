# Robot management API and development dashboard

One aiohttp application, on its own port, with its own token, in
`main/nilo-server/robot/api/`. It starts with the server and binds to loopback.

```bash
export NILO_ROBOT_ADMIN_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python app.py
open http://127.0.0.1:8010/            # the dashboard; paste the token into its header
curl -H "Authorization: Bearer $NILO_ROBOT_ADMIN_TOKEN" http://127.0.0.1:8010/api/robots
```

* [What changed: this API can move a robot](#what-changed-this-api-can-move-a-robot)
* [Security](#security)
* [Endpoints](#endpoints)
* [The event stream](#the-event-stream)
* [The dashboard](#the-dashboard)
* [The simulator panel](#the-simulator-panel)
* [Configuration](#configuration)
* [Testing](#testing)

## What changed: this API can move a robot

Through the memory phase, the admin API deliberately had **no** endpoint that could
actuate. An admin surface that could drive a robot would be a second path to the hardware,
and the authentication story was not finished.

It is now, and a development dashboard that cannot move the robot it is debugging is not a
development dashboard. What has **not** changed is where the decision is made:

* every control request becomes a typed action spec, submitted to the same executor,
  attributed to `ActionSource.USER`, and judged by the same safety policy as a behaviour's
  own command;
* there is no privileged path — an operator holding the admin token cannot obtain what the
  policy would refuse a model;
* nothing under `main/nilo-server/robot/api/` calls a device, and the package cannot even
  import `main/nilo-server/robot/safety/`. A test reads the source and asserts both.

An emergency stop from this API is a **request**. The response says so. The firmware
watchdog is the guarantee ([safety.md](safety.md)).

## Security

Three independent gates, in `main/nilo-server/robot/api/security.py`:

| Gate | Applies to | What it is |
|---|---|---|
| Token | everything but `/health` and `/api/meta` | a bearer token, compared in constant time, **distinct from the device-token signing key** |
| Bind | control endpoints only | refused outright when the API is not on loopback, unless explicitly allowed |
| Rate limit | control endpoints only | 30 requests per minute per client by default |

The token is separate from the device key because the OTA endpoint is unauthenticated and,
with auth enabled, will mint a valid device token for whatever device id the caller asks
for (`main/nilo-server/core/api/ota_handler.py`). "The caller holds a valid device token"
authorizes nothing here.

The bind gate exists because a token in a shell history is a token. Binding wider than
loopback to read telemetry from a laptop is a reasonable thing to want; it should not
silently also publish the endpoint that drives the robot across the room. Reads are
unaffected by it.

With no token configured the API **fails closed**: every authenticated route returns 503
rather than serving. That is the default, so a server started without thinking about it
does not expose a robot.

## Endpoints

Read — token only:

| Method | Path | What it returns |
|---|---|---|
| `GET` | `/health` | liveness. Unauthenticated, and says nothing about any robot |
| `GET` | `/api/meta` | what this binding allows. No robot data |
| `GET` | `/api/robots` | every robot: identity, connection, session |
| `GET` | `/api/robots/{id}` | the above plus memory counts, autonomy mode, e-stop state |
| `GET` | `/api/robots/{id}/state` | telemetry: pose, motion, battery, sensors, audio, vision, expression, activity |
| `GET` | `/api/robots/{id}/capabilities` | what the device published, as discovered |
| `GET` | `/api/robots/{id}/actions` | actions, newest first |
| `GET` | `/api/robots/{id}/behavior` | the last decision, with every candidate's score |
| `GET` | `/api/robots/{id}/world` | tracked entities, with ages |
| `GET` | `/api/robots/{id}/memory` | working, episodic, semantic and person memory |
| `GET` | `/api/robots/{id}/memory/recall` | what would actually go in front of a model |
| `GET` | `/api/robots/{id}/personality` | traits and the internal control variables |
| `GET` | `/api/robots/{id}/conversation` | the transcript, the audio state, who is speaking |
| `GET` | `/api/robots/{id}/tools` | every robot tool, and whether it is usable right now |
| `GET` | `/api/robots/{id}/animations` | the animations this robot can play |

Control — token, loopback and rate limit:

| Method | Path | Body |
|---|---|---|
| `POST` | `/api/robots/{id}/actions/move` | `{"distance_mm": 250, "speed_mmps": 200}` |
| `POST` | `/api/robots/{id}/actions/turn` | `{"angle_deg": 90, "speed_dps": 90}` |
| `POST` | `/api/robots/{id}/actions/stop` | `{"reason": "..."}` |
| `POST` | `/api/robots/{id}/actions/head` | `{"pitch_deg": 10, "yaw_deg": 0}` |
| `POST` | `/api/robots/{id}/actions/look_at` | `{"x_pct": 50, "y_pct": 50}` |
| `POST` | `/api/robots/{id}/actions/lift` | `{"height_pct": 40}` |
| `POST` | `/api/robots/{id}/expression` | `{"emotion": "happy"}` |
| `POST` | `/api/robots/{id}/animations/{name}` | `{}` |
| `POST` | `/api/robots/{id}/autonomy-mode` | `{"mode": "passive"}` |
| `POST` | `/api/robots/{id}/emergency-stop` | `{"reason": "..."}` |
| `DELETE` | `/api/robots/{id}/emergency-stop` | clears the latch |
| `DELETE` | `/api/robots/{id}/actions` | cancels everything in flight |
| `POST` | `/api/robots/{id}/simulate/{event}` | see [the simulator panel](#the-simulator-panel) |

Status codes that carry meaning:

| Code | Means |
|---|---|
| `202` | the robot **accepted** the command. It has not finished it |
| `400` | the body did not validate. Unknown fields are errors, not silent defaults |
| `403` | control is disabled on this binding |
| `409` | the safety policy refused it. The body carries the typed `reason` |
| `429` | rate limited |

```json
{"error": "a cliff is detected", "reason": "cliff_hazard", "status": "rejected"}
```

Bodies are frozen pydantic models with `extra="forbid"`. Ranges are deliberately **not**
duplicated in the API: the safety policy owns the ceilings, and a request past one is a
409 rather than a 400. One place decides what is allowed.

`GET /api/openapi.json` is generated from those same models, and a test asserts that every
route the application registers appears in it.

## The event stream

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8010/api/events?categories=action,world"
```

Server-sent events rather than a WebSocket: the traffic is one-directional, SSE reconnects
on its own, and it is a plain HTTP response — so it uses the same auth header, middleware
and test client as everything else. A WebSocket would need a second auth path, and the
token would end up in a query string.

Categories: `telemetry`, `action`, `behavior`, `world`, `conversation`, `error`, `system`.
An unknown category is a 400, not an empty stream — a dashboard that asked for `behaviour`
and got nothing would look like a broken robot. An event type nobody has mapped lands in
`system` rather than being dropped, so a new event shows up in the stream on the day it is
added.

Each subscriber has its own bounded queue and a slow reader loses its **oldest** events,
never the publisher's time. A dropped telemetry frame is not worth stalling a robot for.

## The dashboard

`GET /` — one HTML file, no build step, no framework, no CDN, no `package.json`. It looks
like a development tool because it is one.

Panels: connected robot · battery · pose · sensors · current action · current behaviour ·
behaviour candidate scores · internal control variables · world entities · known people ·
recent memories · available robot tools · conversation · recent events.

Controls: move · turn · stop · head · lift · animation · expression · autonomy mode ·
emergency stop.

The token lives in `localStorage` and travels in an `Authorization` header — including on
the event stream, which is read with `fetch` and a `ReadableStream` rather than
`EventSource`, because `EventSource` cannot set a header and the alternative is a token in
a query string, in browser history, and in every access log between here and there.

## The simulator panel

```
POST /api/robots/{id}/simulate/person       {"person_id": "ahmad", "x": 0.4}
POST /api/robots/{id}/simulate/object       {"label": "cube"}
POST /api/robots/{id}/simulate/obstacle     {"detected": true}
POST /api/robots/{id}/simulate/cliff        {"detected": true}
POST /api/robots/{id}/simulate/touch        {"detected": true}
POST /api/robots/{id}/simulate/low_battery  {"percent": 8}
```

Every injection goes in through **the same doors real perception uses** — a telemetry patch
through the runtime, or a world entry plus the event the vision pipeline publishes — so the
world model, the behaviour engine, the safety policy and the agent see exactly what they
would see from a device. There is no "simulated" flag downstream, because a simulation that
takes a different path through the code is a simulation of different code.

Injection is a **control** operation, not a read. Telling the server there is no obstacle in
front of a robot that is about to drive is as dangerous as driving it, so it sits behind the
same three gates.

Sensor injections are sticky until cleared. One consequence: a connected device that reports
the same slice overwrites an injection on its next telemetry frame — it is telling the truth
and the injection is not. Injection is for what a device does *not* report.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `NILO_ROBOT_ADMIN_TOKEN` | unset | the bearer token. Unset means the API fails closed |
| `NILO_ROBOT_API_HOST` | `127.0.0.1` | bind address |
| `NILO_ROBOT_API_PORT` | `8010` | port |
| `NILO_ROBOT_API_ALLOW_REMOTE_CONTROL` | unset | allow control endpoints off loopback |

None of these live in the server config dict, and that is deliberate: in manager-api mode
the local configuration is replaced wholesale by the API response
(`main/nilo-server/config/config_loader.py`), and a credential that vanishes in the
deployment with the most robots in it is not a credential.

The API task is supervised. `app.py` creates its tasks and never inspects them, so a port
conflict would otherwise degrade the server silently.

## Testing

```bash
pytest tests/robot/test_api.py tests/robot/test_api_control.py tests/robot/test_api_events.py -q
```

`main/nilo-server/tests/robot/test_api_control.py` runs against a **real simulated robot**
(`main/nilo-server/tests/robot/simulated.py`), so "the robot did not move" is a claim about
a pose rather than about a mock. Covered: every gate (token, loopback, rate limit), request
validation including invented fields and floats, a missing robot, an unsafe move at a cliff
refused with its typed reason, a distance past the configured limit, the emergency stop
latching and clearing, cancellation, and every injection.

`main/nilo-server/tests/robot/test_api_events.py` covers the stream — delivery, category and
robot filters, an unknown category, a slow reader losing its oldest events rather than
stalling the publisher, and every event type having a category — plus the OpenAPI document
covering every registered route, and the dashboard's panels, controls and token handling.

---

[robot-architecture.md](robot-architecture.md) — why this is its own app on its own port ·
[safety.md](safety.md) — what a stop from this API is and is not ·
[robot-actions.md](robot-actions.md) — the action layer every control request ends at ·
[robot-memory.md](robot-memory.md) — what the memory endpoints read and delete
