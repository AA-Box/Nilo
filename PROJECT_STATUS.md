# Project status

What actually runs, what half-runs, what is not built, and what cannot be judged without
hardware. The rule for this page: **nothing is called working unless a passing test
demonstrates it**, and every claim below names the test that does.

Generated against the tree at the time of writing. Reproduce it with:

```bash
make lint && make typecheck && make check-docs && make test
cd main/nilo-server && pytest tests/e2e -q && open tmp/e2e-report.md
```

Suite sizes today: **1118** unit tests (`tests/robot/`), **35** integration tests against a
real server and socket (`tests/integration/`), **30** end-to-end tests
(`tests/e2e/`), plus the inherited server's own (`tests/core/`, `tests/config/`,
`tests/plugins/`). No test needs a network, a model download or a cloud account.

---

## WORKING

Each row has a test that fails if the claim stops being true.

### Device and session

| Capability | Demonstrated by |
| :--- | :--- |
| A device connects, is registered, and gets a session id | `tests/e2e/test_scenarios.py::test_scenario_a_startup` |
| Device MCP handshake and paged `tools/list` discovery | `tests/integration/test_simulator_e2e.py::test_capabilities_arrive_across_several_tool_pages` |
| Telemetry notifications become world state | `test_telemetry_notifications_reach_the_world_state` |
| Two robots in one process stay two robots | `test_two_robots_are_two_entries` |
| A disconnect clears the registry entry and the capabilities | `test_an_abrupt_disconnect_clears_the_registry_entry` |
| A reconnect is a new session, and rediscovers | `tests/e2e/test_scenarios.py::test_scenario_j_reconnect` |
| An unpublished tool is refused before it reaches the device | `test_unpublished_tool_is_refused_before_it_reaches_the_device` |
| Malformed frames do not break a session | `tests/e2e/test_robustness.py::test_malformed_frames_do_not_break_the_session` |
| A duplicate motion completion settles nothing twice | `test_a_duplicate_completion_settles_nothing_twice` |
| A completion for an unknown action is dropped | `test_a_completion_for_an_unknown_action_is_dropped` |
| Eight connect/disconnect cycles leak no tasks or subscriptions | `test_repeated_connect_disconnect_leaks_nothing` |
| A reconnect racing a previous session's teardown keeps the newer one | `test_reconnect_while_a_previous_session_is_still_closing` |

### Actions and safety

| Capability | Demonstrated by |
| :--- | :--- |
| Ten semantic actions, typed, validated, queued, dispatched | `tests/robot/test_actions.py`, `tests/robot/test_executor.py` |
| A move runs end to end and the robot actually moves | `tests/integration/test_actions_e2e.py` |
| Safety is evaluated at submission *and* again at dispatch | `tests/robot/test_safety.py` |
| Rejections are typed reasons, not booleans | `tests/robot/test_safety.py` |
| A model cannot override a safety refusal | `tests/e2e/test_scenarios.py::test_scenario_f_cliff_stops_the_robot` |
| Stale telemetry stops being acted on | `tests/e2e/test_robustness.py::test_stale_telemetry_stops_being_acted_on` |
| A device that never reports completion times out rather than hanging | `test_a_silent_device_times_the_action_out_rather_than_hanging` |
| Cancel racing a completion settles exactly once | `test_cancel_racing_a_completion_settles_once` |
| Resource conflicts and priority preemption | `tests/robot/test_queue.py` |
| Emergency stop latches, cancels, and is never queued | `tests/robot/test_safety.py` |

### Autonomy

| Capability | Demonstrated by |
| :--- | :--- |
| Sixteen built-in behaviours, utility-scored, with an explainable decision | `tests/robot/test_behaviors.py`, `tests/robot/test_behavior_scheduler.py` |
| A person appears, vision sees them, the robot greets them once | `tests/e2e/test_scenarios.py::test_scenario_d_autonomous_greeting` |
| Boredom rises with idle time and wins a low-risk behaviour | `test_scenario_e_boredom` |
| Low battery outranks the sociable behaviours | `test_scenario_g_low_battery_outranks_everything` |
| Autonomy modes gate what may run, and lowering one cancels | `tests/robot/test_autonomy.py` |
| The engine keeps deciding with no model present | `test_scenario_i_the_robot_survives_a_dead_model` |
| Tuning lives in one file, and a test parses the source to prove it | `tests/robot/test_behaviors.py` |

### Conversation and voice

| Capability | Demonstrated by |
| :--- | :--- |
| An utterance becomes an answer, spoken through one speech path | `tests/e2e/test_scenarios.py::test_scenario_b_conversation` |
| "Come a little closer" becomes a validated, safety-checked action | `test_scenario_c_voice_commanded_movement` |
| Barge-in: speaking → interrupted → listening, and the next turn works | `test_scenario_h_barge_in` |
| The audio state machine refuses illegal transitions | `tests/robot/test_voice_loop.py` |
| Two things never speak at once | `tests/robot/test_voice_loop.py` (`max_concurrent`) |
| A missing model is a sentence, not an exception | `tests/robot/test_agent.py`, `test_scenario_i_...` |
| Tool permissions by class and turn origin | `tests/robot/test_agent_tools.py` |

### Vision, memory, personality

| Capability | Demonstrated by |
| :--- | :--- |
| Snapshot pipeline: capture → decode → detect → track → world model | `tests/robot/test_vision.py` |
| A local detector that reads the simulator's actual pixels | `tests/e2e/test_scenarios.py::test_scenario_d_autonomous_greeting` |
| Four memory stores over SQLite, with merge-not-overwrite semantics | `tests/robot/test_memory.py` |
| Personality biases proposals and can never reach safety | `tests/robot/test_personality.py`, `tests/robot/test_layering.py` |
| Animations are YAML data; adding one needs no Python | `tests/robot/test_animation.py` |

### Operations

| Capability | Demonstrated by |
| :--- | :--- |
| Management API on its own port, token, loopback rule and rate limit | `tests/robot/test_api_control.py` |
| The API can move a robot, and only through the same executor | `tests/robot/test_api_control.py`, `tests/robot/test_layering.py` |
| Metrics for connections, tools, actions, model, speech, vision, behaviour | `tests/e2e/test_observability.py` |
| One correlation id from utterance to device response, in events and logs | `test_one_utterance_is_one_trace_from_words_to_device_response` |
| `/health`, `/ready` unauthenticated; `/metrics` is not | `test_the_metrics_endpoint_is_authorized_and_the_probes_are_not` |
| A closed runtime reports not-ready | `test_a_closed_runtime_is_not_ready` |
| Closing the runtime leaves no tasks, threads or subscriptions | `tests/e2e/test_robustness.py::test_closing_the_runtime_leaves_nothing_running` |
| The simulator: a full device on a real socket, with injectable faults | `tests/robot/test_simulator.py`, `tests/integration/` |

---

## PARTIALLY WORKING

Real code, real tests, and a named limit. None of these is a stub.

| Area | What works | What does not, and why |
| :--- | :--- | :--- |
| **Vision detectors** | The pipeline, the tracker, the world fold, a null detector, a colour-blob detector for the simulator, and a YOLO adapter | The YOLO path has never run in CI — Ultralytics is not a test dependency. The default finds nothing, on purpose: a deployment with no model should be boring, not broken |
| **Face recognition** | The registry, the embedding comparison, the identity plumbing through tracks and events | There is no face *embedder*. Recognition works against embeddings something else produced; nothing in this repository produces them |
| **Charger docking** | `go_to_charger` wins arbitration at critical priority and drives toward a known dock | Nothing promotes a *detected* dock to a world location, so the robot only knows where a dock is if something tells it. Scenario G supplies that knowledge and asserts on the arbitration, which is the half that exists |
| **ASR latency metric** | Recorded and histogrammed for sessions that run the inherited recognizer | A device that does its own recognition reports no latency; `0` means "not measured" |
| **TTS metrics** | Stream duration and time-to-first-audio, both from real events | Neither measures the vocoder. Synthesis time is inside the inherited provider, which does not report it |
| **Memory consolidation** | Episodic → semantic consolidation runs, merges rather than overwrites, and is tested | It has never run against months of data. The retention story is a policy nobody has written |
| **Personality persistence** | Snapshots written on detach and restored on reconnect | Opt-in; a runtime with no store keeps personality in memory and loses it on restart |
| **Multi-robot** | Two robots in one process are two registries, two engines, two personalities, and a test proves it | Nothing has run more than a handful at once. There is no back-pressure story for fifty |

---

## NOT IMPLEMENTED

Named because they are absent, not because they are planned for next week.

| Missing | Consequence |
| :--- | :--- |
| Navigation and mapping | The robot drives in straight lines and turns in place. There is no path planner, no map, no localization beyond odometry. "Go to the kitchen" is not a thing it can do |
| Obstacle avoidance in the backend | Obstacles stop a motion; nothing routes around one |
| A face embedder | See above |
| Speaker identification | The voice loop threads a `person_id` through everything and takes it from the inherited voiceprint provider. With no provider configured, every utterance is from nobody |
| Multi-turn task planning | The agent is capped at three model rounds per turn, deliberately. It cannot carry a plan across turns |
| Sound localization | `react_to_sound` reacts; it does not turn toward the sound, because nothing reports a bearing |
| Fleet management | One process, one set of robots, one config file. There is no cross-process registry, no leader election, no sharding |
| An upgrade path for stored data | The SQLite schema has migrations; the *semantics* of an old personality snapshot against new tuning are undefined |
| Rate limiting on the device channel | A device that floods notifications is bounded only by the event bus's drop-oldest queues |

---

## REQUIRES HARDWARE

These cannot be judged from this repository at all. The simulator is a model of a robot,
and every row below is a place the model could be wrong.

| Claim | Why the simulator cannot settle it |
| :--- | :--- |
| Motion accuracy | The simulator integrates a velocity. A real drive train has wheel slip, carpet, a battery that sags under load, and an IMU that drifts |
| Cliff and obstacle sensing | Simulated sensors are exact and instant. Real ones have a field of view, a minimum range, and a failure mode on black carpet and glass |
| The firmware safety supervisor | Tested in the firmware repository on the host (`Nilo-esp32/host/`), never on the chip in this repository's CI |
| Audio in a room | Echo cancellation, barge-in with the robot's own speaker audible to its own microphone, and a wake word at three metres |
| Timing under real load | Every timing number here was measured with an accelerated clock and a loopback socket |
| Battery and thermals | The simulated battery drains linearly and never gets hot |
| `robot.follow.target` and `robot.audio.set_volume` | Published by the simulator, **not implemented in the firmware**. A real robot refuses both today |
| Camera quality | The simulator renders flat-coloured boxes. A real frame is a photograph, and the answer for a photograph is a model this repository does not ship |
| Recovery from a fall, a stall, or a wheel off the ground | Modelled as `picked_up` and `motor_failure` flags, which is a caricature of each |

---

## How to check any claim on this page

Every row names a test. Run it:

```bash
cd main/nilo-server
pytest tests/e2e/test_scenarios.py::test_scenario_c_voice_commanded_movement -q
```

Or run the ten scenarios and read what actually happened:

```bash
pytest tests/e2e -q && cat tmp/e2e-report.md
```

If a row's test passes and the claim still feels wrong, the test is the bug. Say so in an
issue.
