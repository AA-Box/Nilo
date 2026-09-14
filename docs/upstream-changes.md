# Upstream changes

Every line of inherited code this repository has touched, why, and what a rebase should do
with it. [upstream.md](upstream.md) is the provenance and the sync procedure; this page is
the patch list.

**It is enforced.** `main/nilo-server/tests/robot/test_upstream_seam.py` scans the inherited
directories for anything that reaches into `robot/` and fails when the set differs from the
table below. A hook that is added without being written down here does not reach `develop`.

## The shape of the fork

| Category | Files | What a rebase does |
| :--- | ---: | :--- |
| Robot hooks into inherited files | 9 | resolve by hand; each is 1–5 lines and listed below |
| Nilo-owned new files in inherited directories | 1 | keep ours; conflicts with nothing |
| Translation of Chinese comments, log lines and prompts | ~140 | **take upstream, re-translate** |
| Removed upstream components | 5 trees | keep them removed ([migration.md](migration.md)) |
| Everything else | — | take upstream |

The translation is the large number and the small problem: it touches comments and strings,
never control flow, so a conflicting hunk is resolved by taking upstream's line and
translating it again. The nine hooks are the small number and the real work.

## The nine hooks

Each is the smallest thing that could work, and each calls into a `robot/` function that
**never raises** — a robot failure must not break a voice session for a device that is not
a robot.

### 1. `core/websocket_server.py` — the WebSocket path gate

```python
from robot.protocol import registry_from_config
```

The pre-handshake gate answers `404` for a path no enabled protocol claims. Upstream
hard-codes its own path; the registry makes the route table configuration
([protocol.md](protocol.md)).

**On a rebase:** upstream changing the gate is a real conflict. Keep the registry call and
apply upstream's logic around it.

### 2. `core/http_server.py` — the HTTP route table

```python
from robot.protocol import registry_from_config
```

Same reason, for the OTA and vision routes.

### 3. `core/api/ota_handler.py` — the OTA endpoint's advertised WebSocket URL

```python
from robot.protocol import registry_from_config
```

The URL a device is told to connect to has to be the path the gate accepts. One source.

### 4. `core/connection.py` — the session lifecycle

```python
from robot.session import attach_connection as robot_attach, detach_connection as robot_detach
...
await robot_attach(self)     # after the headers are parsed
await robot_detach(self)     # in the finally
```

Where a connected device becomes a registered robot. Two calls, both of which swallow their
own failures.

**On a rebase:** `core/connection.py` is the file upstream changes most. The hook is two
lines at two well-known points (after header parsing, and in the teardown `finally`); move
them, do not merge them.

### 5. `core/handle/receiveAudioHandle.py` — an utterance reaches the robot agent

```python
from robot.voice import handle_utterance as robot_utterance

if await robot_utterance(conn, processed_text):
    return
```

Placed *after* the plugin interceptors and *before* `conn.chat`, so a robot session is
answered by the robot agent and every other session is unchanged. Returns `False` for a
device that did not publish the robot motion tools.

### 6. `core/handle/abortHandle.py` — barge-in

```python
from robot.voice import handle_barge_in as robot_barge_in

await robot_barge_in(conn)
```

At the end of the inherited abort, after the queues are cleared. The voice loop keeps the
conversation and starts listening again ([robot-voice.md](robot-voice.md)).

### 7. `core/providers/tools/device_mcp/mcp_handler.py` — telemetry notifications

```python
from robot.session import handle_notification as robot_notification  # Nilo robot seam

if await robot_notification(conn, payload):
    return
```

In the branch that otherwise logs an unhandled `method` and drops the frame. Claims only
`notifications/*` that the robot subsystem owns ([robot-protocol.md](robot-protocol.md)).

### 8. `core/providers/asr/base.py` — recognition latency

```python
conn.nilo_asr_latency_ms = total_time * 1000.0
```

One line, one assignment, no import. `handle_voice_stop` already computes the elapsed time
and only logs it; this is the only measurement of ASR latency in the process, and there is
no robot-side hook that can see it ([observability.md](observability.md)).

**On a rebase:** if the line has moved, put it back immediately before the handoff to
`startToChat`. If it has gone, the metric goes quiet — which is a metric with no samples,
not a broken server.

### 9. `config/logger.py` — the version in every log line

```python
from robot import __version__ as SERVER_VERSION
```

The inherited module read its own version constant. `robot/__init__.py` is where the
server's version lives now.

## The one added file

`plugins_func/functions/robot_tools.py` registers the robot tools with the inherited
plugin system. It is a *new* file in a directory upstream owns, which is exactly what
`plugins_func/` is for: `@register_function` modules are discovered, not listed. It
conflicts with nothing.

## Rules that keep this list short

* **New robot functionality goes under `robot/`.** Attach through the smallest possible
  hook, and prefer an existing one to a new one.
* **A hook is a call, never logic.** Every one above is an import and one to three calls.
  If a hook needs a branch, the branch belongs in the `robot/` function it calls.
* **A hook never raises.** The function on the other side catches everything and returns a
  boolean. That is what lets these sit in the inherited path with no `try` around them.
* **`robot/` does not import `core/` at module scope.** Every import of `core` inside
  `robot/` is inside a function, so `import robot` works in a process with no config file
  and no loguru ([robot-architecture.md](robot-architecture.md) R12).
* **New providers are new files.** A vendor adapter under `core/providers/<kind>/<name>.py`
  is additive and never conflicts.

## Doing a rebase

```bash
./scripts/sync-upstream.sh          # advance the vendored baseline
git merge upstream                  # three-way, with a real merge base
```

Then, in this order:

1. **Take upstream for anything with no hook.** The list above is the complete set of files
   that have one.
2. **Re-translate.** A conflict in a comment or a log line is upstream's text; take it and
   translate it. `make check-docs` fails on Chinese prose that survives.
3. **Re-place the nine hooks.** Each section above says where its hook goes and what it
   must sit after.
4. **Run the enforcement.**

```bash
cd main/nilo-server && pytest tests/robot/test_upstream_seam.py -q
```

That test fails if a hook was lost, if one was added, or if this page stopped describing
the tree. Then the full suite, which is what proves the seams still work:

```bash
make lint && make typecheck && make test && make e2e
```

## Related pages

* [upstream.md](upstream.md) — provenance, the vendor branch, and what was removed
* [migration.md](migration.md) — what was removed and why
* [robot-architecture.md](robot-architecture.md) — the layering rules the hooks obey
* [architecture-final.md](architecture-final.md) — the whole system, including these seams
