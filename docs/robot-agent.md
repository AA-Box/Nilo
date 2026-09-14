# Robot agent — the LLM seam

The agent is the language model with a body attached, and everything that keeps it out of
trouble. It lives in `main/nilo-server/robot/agent/`.

```python
from robot.agent import RobotAgent, SpeakIntent, ToolPolicy

agent = await runtime.agent("nilo-sim-01")
turn = await agent.respond("come closer", person_id="ahmad")
turn.text            # "Okay."
turn.tool_calls      # ("robot_move",)
turn.refusals        # () — or the typed reason safety gave
```

* [What the model owns, and what it does not](#what-the-model-owns-and-what-it-does-not)
* [The tools](#the-tools)
* [Permissions](#permissions)
* [The context](#the-context)
* [Conversation and interruption](#conversation-and-interruption)
* [Proactive speech](#proactive-speech)
* [When the model is unavailable](#when-the-model-is-unavailable)
* [Wiring](#wiring)
* [Testing](#testing)

## What the model owns, and what it does not

| The agent owns | It does not own | Where that lives |
|---|---|---|
| conversation | PID loops, raw motors | firmware ([safety-model.md](safety-model.md)) |
| semantic interpretation | the safety policy | `main/nilo-server/robot/safety/` |
| high-level planning | behaviour scheduling | `main/nilo-server/robot/behavior/` |
| tool selection | vision tracking loops | `main/nilo-server/robot/vision/` |
| wording the answer | timing-critical operations | the action watchdog, on its own thread |

This is not a preference about where code should go. A language model is a component that
is occasionally wrong, occasionally slow and occasionally unavailable. Each of those is
survivable for a conversation and fatal for a control loop, so the control loops have no
model in them and keep running when it is gone.

**There is one LLM stack, not two.** `robot/agent/` wraps the inherited provider
architecture: anything with `response_with_functions` is a provider, which is every module
under `main/nilo-server/core/providers/llm/` and a twenty-line fake in a test. No second
client, no second retry policy, no second place that knows an API key.

## The tools

Fourteen semantic tools, in `main/nilo-server/robot/agent/tools.py`. Each has a flat wire
name (the tool namespace is flat and a function name may not contain a dot) and a dotted
name the documentation uses.

| Tool | Permission | What it does |
|---|---|---|
| `robot.move` | motion | drive straight; negative distances drive backwards |
| `robot.turn` | motion | turn in place; positive angles turn left |
| `robot.stop` | motion | stop. Never refused by the policy |
| `robot.look_at` | motion | point the head at a spot in the camera frame |
| `robot.follow_person` | motion | follow somebody for a bounded time |
| `robot.stop_following` | motion | cancel the follow in flight |
| `robot.play_animation` | expressive | play a pre-authored animation |
| `robot.set_expression` | expressive | show a face from the closed expression set |
| `robot.capture_image` | privileged | take one picture and describe it |
| `robot.inspect_object` | privileged | take one picture and ask a question about it |
| `robot.get_battery` | read-only | battery percentage and charging state |
| `robot.get_state` | read-only | activity, motion, battery, what is visible |
| `robot.remember` | privileged | write one thing into long-term memory |
| `robot.recall` | read-only | look something up in long-term memory |

Four properties hold for all of them:

* **Nothing reaches a device from here.** Motion, head, face and camera tools go through
  `main/nilo-server/robot/actions/semantic.py`, which is the executor, which is the only
  thing in the process that talks to hardware. Memory tools go through
  `main/nilo-server/robot/memory/service.py`. There is no third path, and
  `main/nilo-server/tests/robot/test_layering.py` proves the agent cannot even import one.
* **The vocabulary is semantic.** Integers with the unit in the name, no floats, and no
  parameter that names an actuator. A test enumerates every schema and fails on `pwm`,
  `duty`, `servo_us`, `wheel_speed`, `voltage`, `motor` and friends, and a second one
  fails on any parameter whose JSON type is `number`.
* **Arguments are validated before anything is submitted.** Frozen pydantic models with
  `extra="forbid"`, and integer bounds taken from the **live** safety limits — so the
  ceiling in the schema the model reads and the ceiling the policy enforces are the same
  number. An invented parameter is an error the model can read, not a silently ignored
  key.
* **Motion never waits.** Handlers submit and return. The inherited chat loop awaits tool
  futures sequentially on a five-worker pool with no cancellation
  (`main/nilo-server/core/connection.py`), so a handler that waited for a robot to finish
  driving would pin a worker for the whole `tool_call_timeout`.

A refusal is a result, not an exception. When safety rejects an action the model is handed
the typed reason (`safety:cliff_hazard`), so the robot can say *why* it will not move
instead of inventing an explanation.

### Distances are integers in millimetres

`robot.move(distance_mm=250)`, not `distance_m=0.25`. The device MCP type system carries
booleans, integers and strings; nothing on the server would reject a float and the
firmware cannot express one, so a float parameter is a latent bug ([mcp.md](mcp.md)).

## Permissions

Four classes, in `main/nilo-server/robot/agent/permissions.py`:

| Class | Examples | Default policy |
|---|---|---|
| `READ_ONLY` | battery, state, recall | autonomous |
| `EXPRESSIVE` | expression, animation | autonomous |
| `MOTION` | move, turn, look_at, follow | subject to the autonomy mode |
| `PRIVILEGED` | capture_image, inspect_object, remember | only on a turn a person started |

The classification belongs to the tool; the policy over the classes is configuration:

```python
ToolPolicy()                                       # the defaults above
ToolPolicy(autonomous=frozenset())                 # no autonomous tool use at all
ToolPolicy(motion_min_autonomy=AutonomyMode.FULL)  # the robot only drives in FULL
ToolPolicy(motion_min_autonomy=AutonomyMode.OFF)   # a person may always drive it
```

The distinction the policy turns on is **who started the turn**: a person asking the robot
to come closer is not the same event as a behaviour deciding the robot should say hello,
even when both end up calling the same model with the same catalogue.

Two rules are not configurable, because a configuration that can remove them is a
configuration that will:

* **`robot.stop` is always allowed.** A robot that will not stop because its autonomy mode
  is `OFF` is the wrong failure — the same reasoning that makes `StopAction` the one action
  never refused for a hazard ([safety-model.md](safety-model.md)).
* **A permit is not a safety decision.** Everything the policy allows still goes through
  the action layer and its safety policy. This module can only take capability away.

The model is offered exactly the tools the policy would permit on this turn. Advertising a
tool and then refusing it is how a robot ends up apologising for something it was never
going to do.

## The context

`main/nilo-server/robot/agent/context.py` builds the system prompt: identity, current
state, visible world, running behaviour, who is speaking, what the robot remembers, the
permitted tools, and the restrictions in force.

**No raw telemetry history.** The world model holds a current snapshot, not a series, and
the context reads only that snapshot. A context that grows with uptime eventually costs
more than the answer, and a model given four hundred battery readings reasons about the
readings rather than about the battery. Memory contributes the bounded, ranked selection
`robot/memory` already produces, capped again by character count.

`RobotContext.as_dict()` is the same context as data, which is what the management API and
the development dashboard serve.

## Conversation and interruption

`Conversation` holds a bounded transcript, the person speaking, and the turn in flight.

If somebody starts talking while the robot is speaking, `agent.interrupt()`:

1. stops consuming the model stream,
2. **keeps** the conversation, and
3. records what was said before the cut, marked `[interrupted]`.

The partial reply stays in the transcript because the robot did say those words out loud.
A model told it said nothing will say them again.

A second person joining does not erase the first: two people in one room are one
conversation, and the transcript gets a note about who is speaking now.

## Proactive speech

The behaviour engine does not call the model. It asks:

```python
await runtime.request_speech(
    "nilo-sim-01",
    SpeakIntent(reason="greeting", target_person_id="ahmad", style="excited"),
)
```

Every source of speech goes through one arbiter
(`main/nilo-server/robot/agent/speech.py`): a behaviour, a safety announcement, an ambient
remark and the answer to a question. The alternative is a robot with several mouths — a
behaviour that calls the model directly races the reply to the person who is mid-sentence,
two of them race each other, and neither knows that the third is a low-battery warning
that should have won.

Four priorities, and the ordering is the design:

| Priority | What it is | Behaviour |
|---|---|---|
| `SAFETY` | "there is a drop in front of me" | interrupts anything |
| `USER_RESPONSE` | the answer to something a person said | interrupts ambient chatter |
| `BEHAVIOR` | the behaviour engine decided this was worth saying | waits its turn |
| `AMBIENT` | idle noise | dropped the moment anything else wants to speak |

A per-reason cooldown keeps a behaviour that re-proposes `greeting` every tick from
greeting you forty times a minute. Safety is never cooled down.

An intent with `text=` set is spoken **verbatim**, with no model in the path: a warning
that can be paraphrased is a warning that can be paraphrased into something else.

A rejected intent is a returned `SpeechDecision`, never an exception. A behaviour that
asked to say hello and was told the robot is already answering a question has not
malfunctioned.

## When the model is unavailable

The robot keeps working. `RobotAgent.respond` returns a fallback line and sets
`turn.fallback` to `unavailable`, `timeout` or `error`; nothing raises into the caller.

Everything that is not conversation is untouched, because none of it has a model in it:
the behaviour engine still ticks, the world model still folds events, the safety policy
still rejects, the watchdog still fires and `robot.stop` still works.

## Wiring

The tools reach the model through the inherited flat tool namespace.
`main/nilo-server/plugins_func/functions/robot_tools.py` is a four-line module in the
directory `auto_import_modules` scans; it calls `register_robot_tools()` from
`main/nilo-server/robot/agent/bridge.py`, which registers all fourteen as `IOT_CTL` server
plugins — the only type exposed to the model without a `config.yaml` edit.

`detect_collisions()` is the other half. The namespace is flat and the device-MCP executor
is registered *after* the server-plugin one, so a device that publishes its own
`robot_move` would replace the guarded one with nothing worse than a logged warning
(`main/nilo-server/core/providers/tools/unified_tool_manager.py`). The firmware vocabulary
is `robot.motion.move`, which sanitizes to `robot_motion_move` and does not collide; a
device that publishes `robot_move` anyway is an error, loudly.

On the runtime:

```python
runtime.set_llm(provider)                  # install a provider for every robot
agent = await runtime.agent(robot_id)      # one agent per robot, created on first use
arbiter = runtime.speech(robot_id, speaker)
await runtime.request_speech(robot_id, intent)
```

## Testing

`main/nilo-server/tests/robot/test_agent.py`,
`main/nilo-server/tests/robot/test_agent_tools.py` and
`main/nilo-server/tests/robot/test_agent_bridge.py`. Every test uses a **scripted** LLM —
no network, no API key, nothing in CI that depends on an external service:

```bash
pytest tests/robot/test_agent.py -q
```

The runtime under them is real: the executor, the safety policy, the queue and the event
bus are the production ones, and only the model and the device are fakes. That is the only
way "the robot behaves correctly when the model asks for something unsafe" can be a test
rather than a hope.

Covered: tool validation, an unavailable model, a tool timeout, an unsafe action rejected,
interruption mid-sentence, memory in the context, conversation continuity across turns,
and behaviour-triggered speech.

---

[robot-architecture.md](robot-architecture.md) — where robot code goes and the safety rule ·
[robot-actions.md](robot-actions.md) — the action layer the tools submit to ·
[safety-model.md](safety-model.md) — what the backend can and cannot promise ·
[robot-memory.md](robot-memory.md) — what `robot.remember` and `robot.recall` write to
