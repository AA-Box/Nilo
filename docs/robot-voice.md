# Robot voice loop

The complete path from a microphone to a robot that moves and answers, in
`main/nilo-server/robot/voice/`.

```
microphone → VAD → ASR → person → RobotAgent → tools/actions → model → TTS → speaker
             └──── inherited ────┘  └── robot/agent ──┘          └── inherited ──┘
```

The ends of that path are the upstream pipeline and are unchanged: Silero VAD, the
streaming ASR providers, the sentence-chunked TTS queue, the Opus encoder and the device
WebSocket ([audio.md](audio.md)). This package is the part in the middle that did not
exist — who is allowed to talk, what the face does while they do, and what happens when
somebody talks over the robot.

* [Audio state](#audio-state)
* [Barge-in](#barge-in)
* [One mouth](#one-mouth)
* [Speech priority](#speech-priority)
* [Expression](#expression)
* [How it attaches](#how-it-attaches)
* [Testing](#testing)

## Audio state

Five states, and a checked transition table:

| State | Meaning |
|---|---|
| `IDLE` | nothing is happening |
| `LISTENING` | a microphone is open and somebody may be talking |
| `THINKING` | the utterance is being answered — the model, and any tools it calls |
| `SPEAKING` | audio is streaming to the robot's speaker |
| `INTERRUPTED` | speech stopped early because somebody talked over it |

Every change publishes an `AudioStateChanged` event, so "what is this robot doing with its
ears and its mouth" is one subscription rather than four flags read in the right order.

That single answer is the point. The inherited session has several — `client_is_speaking`,
`client_abort`, the TTS queue's own `tts_sentence_type`, and whatever the device believes.
None of them is wrong; none is the whole picture either, and a barge-in that reads a
different one from the one the speaker wrote is a robot that talks over the person who
interrupted it.

`SPEAKING → LISTENING` is deliberately **not** a legal transition. Speech that ends because
somebody talked over it goes through `INTERRUPTED` first, so "was that reply cut off?" is a
question the history can answer. An illegal transition is refused and logged, not applied.

## Barge-in

```
robot speaking + a person starts talking
  → the TTS stream is cancelled and the device is told to stop playing
  → SPEAKING → INTERRUPTED → LISTENING
  → the conversation is kept, and the partial reply is recorded as said
```

The partial reply stays in the transcript, marked `[interrupted]`, because the robot did
say those words out loud. A model told it said nothing will say them again.

Stopping is **cooperative first**. The arbiter tells the speaker to stop, gives it
`INTERRUPT_GRACE_S` to settle, and only then cancels the task. A hard cancel first would
unwind the speaker out of the middle of a turn and lose the conversational state that turn
was building — which is the state a barge-in most needs to keep.

## One mouth

Every sentence the robot says goes through one arbiter
(`main/nilo-server/robot/agent/speech.py`) and then one sink behind one lock. Two
simultaneous TTS streams for one robot is not a race this code can lose: the second waits
for the first to let go, and a test asserts the observed concurrency never rises above one.

The lock is not redundant with the arbiter. An intent that preempted another one starts
while the cancelled task is still unwinding out of the sink.

## Speech priority

| Priority | What it is | Behaviour |
|---|---|---|
| `SAFETY` | "there is a drop in front of me" | interrupts anything, never cooled down |
| `USER_RESPONSE` | the answer to something a person said | interrupts ambient chatter |
| `BEHAVIOR` | the behaviour engine decided this was worth saying | waits its turn |
| `AMBIENT` | idle noise | dropped the moment anything else wants to speak |

A safety intent carries its own `text` and is spoken **verbatim**, with no model in the
path: a warning that can be paraphrased is a warning that can be paraphrased into something
else.

## Expression

| Audio state | Face |
|---|---|
| `LISTENING` | attentive (`focused`) |
| `THINKING` | `curious` |
| `SPEAKING` | a subtle `speaking` animation, *if the library has one* |
| `INTERRUPTED` | attentive again — the person is talking |
| `IDLE` | `neutral` |
| a failed turn | `confused` |
| a tool running | `tool_<name>` animation, if the library has one |

**Avoiding constant mechanical animation is the design problem here.** A face driven
directly from an audio state machine changes on every transition, and a robot whose eyes
twitch four times per sentence is worse company than one that does nothing. Three rules:

* Nothing is sent when nothing changed.
* Two transitions inside `MIN_INTERVAL_S` produce one face change, and the **last** one
  wins — `settle()` applies what the rate limit held back when the turn ends, so a fast
  LISTENING → THINKING → SPEAKING run leaves the robot showing the state it ended in.
* A missing animation is silence, not a fallback. No `speaking` animation in the library
  means the face keeps what it had. Substituting something else is how a robot ends up
  looking surprised every time it answers a question.

The coordinator commands through the same semantic handle as everything else, so every
expression it sets is still filtered by the safety policy.

## How it attaches

Two calls into the inherited session, both of which never raise:

```python
# core/handle/receiveAudioHandle.py, in startToChat
if await robot_utterance(conn, processed_text):
    return

# core/handle/abortHandle.py, in handleAbortMessage
await robot_barge_in(conn)
```

A session is claimed only when **both** of these hold:

* the device published the motion tools the action layer dispatches to — the session layer
  registers any device as a robot, and a smart speaker is not a robot; and
* the runtime has an LLM provider — otherwise the inherited chat path still has its own,
  and a fallback line is a worse answer than the one the session was already going to give.

Anything else returns `False` and the upstream path runs exactly as before.

The sink drives the inherited TTS pipeline through its own queue rather than
reimplementing it: `tts_start` puts the FIRST marker, each streamed chunk goes in as a
MIDDLE text message, and `tts_end` puts the LAST one. Sentence splitting, streaming
synthesis, Opus encoding and the send loop are upstream code doing what it already does.

`cancel` performs the same three steps the inherited abort does — set the flag, drop the
queued audio, tell the device to stop — and deliberately does not call `handleAbortMessage`,
which is one of the two places this seam is called *from*.

## Testing

`main/nilo-server/tests/robot/test_voice_loop.py` and
`main/nilo-server/tests/robot/test_voice_seam.py`:

```bash
pytest tests/robot/test_voice_loop.py -q
```

The headline test is the flow from the design note, with every transition and event
asserted:

```
a person says "come closer"
  → fake ASR produces the text
  → the agent calls robot.move(distance_mm=250)
  → the simulated robot executes it: physics, pose, completion notification
  → the robot answers "Okay"
```

Four fakes and one real thing. Fake: the microphone (a list of strings), the ASR, the model
(a script), and the speaker (a recorder that counts overlapping streams). Real: the robot.
`main/nilo-server/tests/robot/simulated.py` is the simulator's own physics, sensor model,
world and tool table with the WebSocket taken out, so `robot.motion.move` starts a motion
that takes time, the pose integrates, an obstacle stops it early, and the completion
notification that settles the action comes back on its own. The executor, the safety
policy, the world model and the event bus are the production ones.

Also covered: a barge-in while TTS is active, the transcript surviving it, the next
utterance being answered afterwards, two streams never overlapping, a behaviour failing to
talk over an answer, a safety line interrupting one, illegal transitions being refused, and
the face not twitching on every transition.

---

[audio.md](audio.md) — the inherited audio pipeline this sits inside ·
[robot-agent.md](robot-agent.md) — the agent the loop drives ·
[robot-animation.md](robot-animation.md) — where the animations come from ·
[safety-model.md](safety-model.md) — what the backend can and cannot promise
