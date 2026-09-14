# Observability

Two questions, and one mechanism for each.

* **"Is it healthy?"** — metrics, at `/metrics` on the management API.
* **"What happened to *that* utterance?"** — one correlation id, in every log line and
  every event of one causal chain.

Both are folds over the event vocabulary the subsystem already publishes
([robot-domain.md](robot-domain.md)). There is no instrumentation scattered through
thirty modules: one subscriber ([`robot/observability.py`](../main/nilo-server/robot/observability.py))
sees every event and turns it into both.

## Correlation: following one utterance

The chain the system has to be able to explain:

```
user utterance -> LLM request -> tool call -> RobotAction -> MCP request -> device response -> completion
```

is one id, carried automatically. `robot/correlation.py` holds it in a
`ContextVar`; every `RobotEvent` stamps the value in scope when it is constructed, and
every `ActionRecord` keeps the id its action was submitted under. Nothing passes a trace id
through a signature.

Where it starts and where it is restored:

| Point | What happens |
| :--- | :--- |
| `VoiceLoop.on_utterance` | a fresh id — one utterance is one trace |
| the agent, its tools, the executor's `submit` | inherited: `asyncio.create_task` copies the context |
| `RobotActionExecutor._dispatch` | re-entered from the action's `correlation_id`, because the pump's task is a different context |
| `_settle` / `_settle_rejected` | re-entered, because a completion arrives on the read loop or the watchdog's hand-off |

A device notification (`MotionCompleted`) carries no trace of its own — it arrives on the
read loop, outside every context. The `ActionFinished` it causes does, and both print the
device's own `device_action_id`, which is the key that joins them.

### The trace in the log

Every correlated event produces one `robot.trace` line in a fixed shape:

```
correlation_id=421eeaf6... event=UtteranceRecognized robot_id=aa-bb-cc-00-00-01 text=come_a_little_closer
correlation_id=421eeaf6... event=AgentTurnStarted    robot_id=aa-bb-cc-00-00-01 turn_id=...
correlation_id=421eeaf6... event=ToolCallStarted     robot_id=aa-bb-cc-00-00-01 call_id=... tool_name=robot_motion_move
correlation_id=421eeaf6... event=ActionStarted       robot_id=aa-bb-cc-00-00-01 action_id=... action_type=move status=starting source=llm
correlation_id=421eeaf6... event=ToolCallCompleted   robot_id=aa-bb-cc-00-00-01 call_id=... tool_name=robot_motion_move
correlation_id=421eeaf6... event=ActionFinished      robot_id=aa-bb-cc-00-00-01 action_id=... status=succeeded device_action_id=dev-3
```

So an incident is `grep correlation_id=<id>` and nothing more. The line is deliberately
one line and a fixed set of fields: dumping a whole event would put a base64 camera frame
in the log.

Turn it off — for a benchmark, or a deployment that ships events elsewhere — by
constructing the runtime with `observe=False`, or the observer with `trace=False`.

## Metrics

`GET /metrics` on the management API returns Prometheus text exposition; `GET /api/metrics`
returns the same numbers as JSON. Both are **authorized as a read**: the labels name
robots, tools and behaviours, which is operational detail about somebody's home.
`/health` and `/ready` are the unauthenticated probes.

```bash
curl -s -H 'Authorization: Bearer $NILO_ROBOT_ADMIN_TOKEN' http://127.0.0.1:8010/metrics
```

There is no client library. The three metric shapes below are the whole surface, the
exposition format is a dozen lines, and `prometheus_client` is not in the development
dependency slice — a metrics module built on it would be one the lint job cannot import.

### What is measured

| Metric | Type | Labels | From |
| :--- | :--- | :--- | :--- |
| `nilo_robot_connected` | gauge | — | `RobotConnected` / `RobotDisconnected` |
| `nilo_robot_sessions_total` | counter | `reconnect` | `RobotConnected` |
| `nilo_robot_disconnects_total` | counter | `reason` | `RobotDisconnected` |
| `nilo_robot_capability_discoveries_total` | counter | `mcp` | `CapabilitiesRefreshed` |
| `nilo_robot_tool_calls_total` | counter | `tool` | `ToolCallCompleted` |
| `nilo_robot_tool_errors_total` | counter | `tool` | `ToolCallFailed` |
| `nilo_robot_tool_refusals_total` | counter | `tool`, `refused_by` | `ToolCallRefused` |
| `nilo_robot_tool_latency_seconds` | histogram | `tool` | the call's own duration |
| `nilo_robot_actions_total` | counter | `action_type`, `status` | `ActionFinished` |
| `nilo_robot_action_latency_seconds` | histogram | `action_type` | dispatch to terminal state |
| `nilo_robot_motions_total` | counter | `kind`, `outcome` | `MotionCompleted` / `MotionFailed` |
| `nilo_robot_llm_turns_total` | counter | `outcome` | `AgentTurnCompleted` / `Failed` |
| `nilo_robot_llm_latency_seconds` | histogram | — | `AgentTurnStarted` to its end |
| `nilo_robot_asr_utterances_total` | counter | — | `UtteranceRecognized` |
| `nilo_robot_asr_latency_seconds` | histogram | — | the recognizer's own elapsed time |
| `nilo_robot_tts_streams_total` | counter | `outcome` | `SpeechFinished` |
| `nilo_robot_tts_latency_seconds` | histogram | — | `SpeechStarted` to `SpeechFinished` |
| `nilo_robot_tts_time_to_first_audio_seconds` | histogram | — | `thinking` to `speaking` |
| `nilo_robot_vision_frames_total` | counter | `outcome`, `provider` | `VisionFrameProcessed` |
| `nilo_robot_vision_latency_seconds` | histogram | — | capture, decode and detection |
| `nilo_robot_behavior_decisions_total` | counter | `behavior` | `BehaviorSelected` |
| `nilo_robot_events_total` | counter | `event` | every event |

Three of those need their definition said out loud, because the obvious reading is wrong:

* **ASR latency is only recorded for sessions that run the recognizer.** It is the one
  number that does not come from an event: `core/providers/asr/base.py` leaves the
  recognizer's elapsed time on the connection and `robot/voice/seam.py` reads it. A device
  that does its own recognition and sends text reports no latency, and `0` means
  "not measured", not "instant".
* **TTS latency is the speech *stream*** — first audio to the end of the utterance — not
  the vocoder. The number a person experiences as the pause before the robot answers is
  `nilo_robot_tts_time_to_first_audio_seconds`, which spans the model *and* synthesis.
* **Action latency is dispatch to terminal state**, so it includes the time the device
  spent physically moving. A 2 s move is a 2 s action and that is correct: the question the
  metric answers is "did this settle", not "was the network fast".

### Cardinality

Label values come from devices and models, so every one is truncated to 64 characters, and
the label sets are closed: a tool name, a behaviour name, a status, a reason. There is no
`robot_id` label anywhere — one household's robot count is not worth a time series per
robot, and the per-robot view is `/api/robots/{robot_id}` already.

The pairing tables that derive latency (a model turn's start waiting for its end, a speech
stream's) are bounded at 256 entries with oldest-out eviction, so a turn whose end never
arrives costs a fixed amount of memory rather than a growing one.

## Health and readiness

| Endpoint | Auth | 200 when | Other |
| :--- | :--- | :--- | :--- |
| `/health` | none | the process is serving | — |
| `/ready` | none | the robot runtime is open | `503` once it is closed |
| `/metrics` | admin token | always | `401` without the token |

`/health` is liveness: the process is up. `/ready` is readiness: this process can still
accept work. They differ exactly once, and it is the case that matters — during shutdown
the process is alive and the runtime is closed, so a rolling deployment should stop sending
it traffic. Neither endpoint says anything about an individual robot.

## Reading the end-to-end report

`pytest tests/e2e` writes `main/nilo-server/tmp/e2e-report.md`: every state transition of
all ten scenarios, with the trace column, taken from the running system's own event bus.
It is the fastest way to see what the chain above looks like in practice, and CI keeps it
as an artifact.

## Related pages

* [robot-domain.md](robot-domain.md) — the event vocabulary this folds over
* [robot-api.md](robot-api.md) — the management API and its three gates
* [testing.md](testing.md) — the suites, including the end-to-end one
* [deployment.md](deployment.md) — scraping this from somewhere other than a laptop
