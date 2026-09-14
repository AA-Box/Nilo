# Engineering review

A review of the robot backend as something to maintain for years rather than to demonstrate
once. What it found, what was fixed, what was deliberately left, and the risks that remain.

No features were added. Everything below is a defect, a duplication, a missing bound or a
missing wire.

## The headline

**Nothing assembled the subsystem.** `get_runtime()` built a bare `RobotRuntime`, and
`app.py` never configured one. Every consequence of that was invisible because every
subsystem worked *in its tests*:

| Documented | Actually, in a running server |
| :--- | :--- |
| `data/robot_limits.yaml` sets the speed ceiling | never read — the defaults, always |
| `data/robot_behavior.yaml` tunes the scheduler | never read |
| Long-term memory over SQLite | no store was ever opened; `robot_remember` always failed |
| Personality persists across restarts | no store was ever installed |
| The voice loop answers a robot's utterances | `runtime.set_llm` was never called, so the seam handed **every** turn back to the inherited chat path |

The last row is the largest: the agent, the tool layer, the permission classes, the speech
arbiter and the voice loop — four phases of work, all tested — were unreachable from
`app.py`.

**Fixed** by a composition root: [`robot/config.py`](../main/nilo-server/robot/config.py)
(one document, one model, one hierarchy) and
[`robot/bootstrap.py`](../main/nilo-server/robot/bootstrap.py) (one function that builds a
runtime from it), called once from `app.py`. The voice seam now takes the provider the
session already has, which is the model that device would have been answered by anyway.

## Findings, by the class the review looked for

### Async races and lifecycle

| Finding | Status |
| :--- | :--- |
| WebSocket reconnect racing a previous session's teardown | already correct — `detach` is a no-op for a superseded session id. Now has an end-to-end test |
| Action completion racing a cancel or a timeout | already correct — one terminal transition, guarded. Now has a repeated-race test |
| Duplicate device completion settling a second action | already correct — matched on the device's id. Now tested over a real socket |
| Behaviour cancellation | already correct — cooperative cancel, then a hard one, resources released |
| TTS cancellation on barge-in | already correct — and scenario H asserts the whole path |
| Simulator shutdown | already correct |
| Leaked tasks across sessions | no leak found; a churn test now proves it for eight cycles |

### Bounds

| Finding | Status |
| :--- | :--- |
| A device could publish unbounded tools — unbounded memory, unbounded metric labels, unbounded prompt | **fixed**: `MAX_TOOLS`, applied on both discovery paths |
| The action rate-limit window was never dropped for a robot that disconnected | **fixed** |
| The management API's rate limiter kept one entry per peer address for ever | **fixed**: eviction plus a ceiling |
| Metric label values unbounded in length | bounded at 64 characters by construction |
| Event bus, action history, conversation, audio history, latency-pairing tables | already bounded |

### Blocking work and thread pools

| Finding | Status |
| :--- | :--- |
| Blocking model generators were pumped on the **default** executor, which is also what `asyncio.to_thread` uses for SQLite — a handful of slow model turns starved every memory query | **fixed**: a dedicated, bounded pool for model pumps |
| SQLite runs off the loop | already correct |

### SQLite

| Finding | Status |
| :--- | :--- |
| One shared connection with `check_same_thread=False`, and the lock was taken by **one** method. `with connection:` commits the *connection's* transaction, so two concurrent statements each committed whatever the other had half-written | **fixed**: every read and write is serialized; `upsert_fact` uses unlocked primitives under its own lock |
| `delete_person` and `clear_robot` were three and four separate transactions — a partial "forget me" was reachable | **fixed**: one transaction, all or nothing, with a rollback test |
| No explicit busy timeout | **fixed**: 5 s, named and documented |

### Correctness

| Finding | Status |
| :--- | :--- |
| Vision could never capture a frame from a real device: `McpFrameSource` asked for the *device's* dotted tool name where the runtime requires the sanitized one | **fixed** (found by scenario D) |
| The speech arbiter's per-reason cooldown applied to answering a person — the robot ignored the second thing you said inside twenty seconds | **fixed** (found by scenario H) |
| `changed_fields` compared timestamped blocks whole, so "changed" meant "present in the patch": identical telemetry woke every subscriber five times a second | **fixed** (found while reading a transcript) |
| `app.py` never closed the robot runtime: the action pump, its watchdog thread and every SQLite handle outlived the process's own shutdown | **fixed** |
| The simulator did not model the firmware's link-loss stop, so "the robot stops locally" was untestable | **fixed** (found by scenario J) |

### Security

| Finding | Status |
| :--- | :--- |
| Token comparison | already constant-time |
| Control endpoints off loopback | already refused unless explicitly enabled |
| The admin token had to be an environment variable | **fixed**: `NILO_ROBOT_ADMIN_TOKEN_FILE`, and both Compose files mount a secret rather than exporting one |
| No token means the API fails closed | already correct, and tested |

### Configuration

Five loaders, five default paths, five answers to "what if the file is missing", and none of
them called by the server. **Fixed**: one document, one model, one hierarchy — defaults,
then `data/robot.yaml`, then the legacy per-area files (with a warning naming the key to
move it under), then environment variables, then a secret file. An unknown key is an error;
a variable that names no field is a warning, because the failure it otherwise produces is
"I set the speed limit and nothing happened".

## Deliberately not changed

* **`RobotToolClient` is a second MCP client used only by tests.** Production discovery and
  dispatch go through `ConnectionToolChannel` over the inherited client. It is ~200 lines of
  parallel JSON-RPC, and it is the only thing that tests request-id isolation and
  `tools/list` pagination without a socket. It stays, named here as a known duplication
  rather than deleted on a review's last day.
* **The translation of inherited comments and log lines.** It is ~140 files of upstream
  diff and the largest rebase cost, and it is also the reason the code is readable. The
  mitigation is procedural: take upstream's line and translate it again
  ([upstream-changes.md](upstream-changes.md)).
* **The `_TURNS` module-level task set in the voice seam.** Process-wide state in a
  subsystem that otherwise has none, kept because the alternative — a per-connection set —
  is a second place to forget to clean up. It is bounded by a done-callback.

## The five biggest remaining risks

1. **The simulator is a model, and the model is optimistic.** Every timing number in this
   repository was measured with an accelerated clock over a loopback socket, and every
   sensor in it is exact and instant. Real range sensors have a field of view, a minimum
   range and a failure mode on black carpet and glass; a real drive train has slip. The
   first contact with hardware will invalidate tuning constants, not architecture — but it
   will invalidate them.

2. **The firmware is the only real safety guarantee, and this repository cannot test it.**
   The backend policy is a filter: the frame that would stop a robot travels on the network
   that has just failed. The five protections that matter live on the chip, are tested on
   the host in the firmware repository, and have never run in this repository's CI. A
   regression there is invisible here.

3. **Two published tools the firmware does not implement.** The simulator publishes
   `robot.follow.target` and `robot.audio.set_volume`; the firmware does not. Everything
   built on the simulator's sixteen tools is built on a contract a real robot does not yet
   meet. Nothing detects the divergence automatically — the two tool lists are compared by
   hand.

4. **Nobody has run this for a week.** The bounds are now in place and the leaks that were
   found are fixed, but the longest continuous run of this system is a two-minute test
   suite. Memory consolidation has never run against months of episodes, SQLite has never
   grown past a few hundred rows, and "does the world model's decay behave over a weekend"
   is an open question with a plausible answer rather than a measured one.

5. **The language model is an unbounded dependency in a bounded system.** Everything else
   here has a ceiling — a distance, a timeout, a queue depth, a token budget. The model has
   a 30-second timeout and three rounds, and beyond that its behaviour is whatever the
   provider does: a prompt that grows with the tool list, a provider that changes its
   streaming shape, a model that decides to call `robot_move` twelve times. The permission
   classes and the safety policy bound the *damage*, not the *cost*.

## Verifying this review

```bash
make lint && make typecheck && make check-docs && make test && make e2e
cd main/nilo-server && pytest tests/robot/test_upstream_seam.py -q   # the fork's seam list
```

## Related pages

* [architecture-final.md](architecture-final.md) — the system, its flows and its boundaries
* [upstream-changes.md](upstream-changes.md) — every hook into inherited code, enforced
* [configuration.md](configuration.md) — the hierarchy this review introduced
* [`PROJECT_STATUS.md`](../PROJECT_STATUS.md) — what works, with the test that proves it
