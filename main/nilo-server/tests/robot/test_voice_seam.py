"""What the seam claims, what it leaves alone, and how it drives the inherited TTS queue.

The gate matters more than the plumbing. The session layer registers *any* device as a
robot, so a seam that claimed every session would take the inherited chat path away from
every smart speaker in the fleet. It claims a session only when the device published the
motion tools the action layer dispatches to, and only when there is a model to answer with.

No socket here either: the connection is an object with the handful of attributes the seam
reads, and the "TTS pipeline" is a list.
"""
from __future__ import annotations

import queue
from typing import Any

import pytest

from robot.runtime import RobotRuntime, set_runtime
from robot.session import RobotSession
from robot.state.models import RobotCapabilities, RobotTool
from robot.voice.seam import (
    LOOP_ATTR,
    ConnectionSpeechSink,
    detach,
    handle_barge_in,
    handle_utterance,
    is_robot_session,
    voice_loop,
)
from tests.robot.conftest import ROBOT_ID, ScriptedLLM, until
from tests.robot.simulated import SimulatedDevice


class FakeTts:
    """The inherited TTS pipeline, as its queue and the three calls the sink makes."""

    def __init__(self) -> None:
        self.tts_text_queue: queue.Queue = queue.Queue()
        self.started = 0
        self.ended = 0
        self.stored: list[tuple[str, str]] = []

    def tts_start(self, conn: Any, sentence_id: str | None = None) -> None:
        self.started += 1

    def tts_end(self, conn: Any, sentence_id: str | None = None) -> None:
        self.ended += 1

    def store_tts_text(self, sentence_id: str, text: str) -> None:
        self.stored.append((sentence_id, text))

    def drain(self) -> list[str]:
        out = []
        while not self.tts_text_queue.empty():
            message = self.tts_text_queue.get()
            if message.content_detail:
                out.append(message.content_detail)
        return out


class FakeWebsocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class FakeSession:
    """A ConnectionHandler, reduced to what the voice seam touches."""

    def __init__(self, robot_id: str = ROBOT_ID) -> None:
        self.session_id = "session-1"
        self.sentence_id = ""
        self.client_abort = False
        self.current_speaker = "Ahmad"
        self.tts = FakeTts()
        self.websocket = FakeWebsocket()
        self.cleared = 0
        self.speak_status_cleared = 0
        setattr(self, "nilo_robot", RobotSession(robot_id, robot_id, "session-1"))

    def clear_queues(self) -> None:
        self.cleared += 1

    def clearSpeakStatus(self) -> None:  # noqa: N802 - the inherited spelling
        self.speak_status_cleared += 1


@pytest.fixture
async def wired():
    """A runtime with a simulated robot and a scripted model, installed process-wide."""
    runtime = RobotRuntime(discovery_timeout=1.0)
    runtime.set_llm(ScriptedLLM("Okay."))
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    set_runtime(runtime)
    try:
        yield runtime, device
    finally:
        set_runtime(None)
        await device.aclose()
        await runtime.aclose()


# -- the gate ---------------------------------------------------------------------------------


async def test_a_device_with_no_robot_tools_is_not_claimed(wired):
    runtime, _ = wired
    await runtime.registry.set_capabilities(
        ROBOT_ID,
        RobotCapabilities(
            mcp=True, tools=(RobotTool(name="lamp_on", raw_name="lamp.on", description="a lamp"),)
        ),
    )
    conn = FakeSession()

    assert await is_robot_session(conn, runtime) is False
    assert await handle_utterance(conn, "hello", runtime) is False


async def test_a_session_with_no_robot_at_all_is_not_claimed(wired):
    runtime, _ = wired
    conn = FakeSession()
    setattr(conn, "nilo_robot", None)

    assert await is_robot_session(conn, runtime) is False
    assert await handle_utterance(conn, "hello", runtime) is False


async def test_a_robot_with_no_model_falls_back_to_the_inherited_chat_path(wired):
    runtime, _ = wired
    runtime.set_llm(None)
    (await runtime.agent(ROBOT_ID)).llm = None
    conn = FakeSession()

    assert await is_robot_session(conn, runtime) is True
    assert await voice_loop(conn, runtime) is None
    assert await handle_utterance(conn, "hello", runtime) is False


async def test_the_loop_takes_the_provider_this_session_already_has(wired):
    """The wiring that makes the voice loop reachable in a real server.

    Nothing in ``app.py`` ever called ``runtime.set_llm``, so the runtime's provider was
    always ``None`` and this seam always handed the turn back to the inherited chat path.
    The session has one — the inherited handler built or was given it — and it is the model
    this device would have been answered by anyway.
    """
    runtime, _ = wired
    runtime.set_llm(None)
    (await runtime.agent(ROBOT_ID)).llm = None
    conn = FakeSession()
    conn.llm = ScriptedLLM("Hello from the session's own model.")

    loop = await voice_loop(conn, runtime)
    assert loop is not None
    assert loop.agent.llm is conn.llm
    assert await handle_utterance(conn, "hello", runtime) is True


async def test_a_real_robot_session_is_claimed(wired):
    runtime, _ = wired
    conn = FakeSession()

    assert await is_robot_session(conn, runtime) is True
    assert await handle_utterance(conn, "hello", runtime) is True
    assert isinstance(getattr(conn, LOOP_ATTR), object)


async def test_the_loop_is_built_once_per_session(wired):
    runtime, _ = wired
    conn = FakeSession()
    first = await voice_loop(conn, runtime)
    assert first is await voice_loop(conn, runtime)
    assert runtime.voice(ROBOT_ID) is first


# -- the turn, through the seam -----------------------------------------------------------------


async def test_an_utterance_reaches_the_inherited_tts_queue(wired):
    runtime, _ = wired
    conn = FakeSession()

    assert await handle_utterance(conn, "hello there", runtime)
    loop = getattr(conn, LOOP_ATTR)
    assert await until(lambda: conn.tts.ended > 0, timeout=3.0)

    assert conn.tts.started == 1
    assert "".join(conn.tts.drain()) == "Okay."
    assert loop.agent.conversation.person_id == "ahmad", "the speaker was not carried through"


async def test_the_utterance_does_not_block_the_read_loop(wired):
    """The caller is the inherited message handler, which the read loop awaits inline."""
    runtime, _ = wired
    conn = FakeSession()
    loop = await voice_loop(conn, runtime)
    loop.agent.llm.delay_s = 0.05

    assert await handle_utterance(conn, "hello", runtime) is True
    assert conn.tts.ended == 0, "handle_utterance waited for the answer"
    assert await until(lambda: conn.tts.ended > 0, timeout=5.0)


# -- barge-in ---------------------------------------------------------------------------------------


async def test_a_barge_in_cancels_the_stream_and_tells_the_device(wired):
    runtime, _ = wired
    conn = FakeSession()
    loop = await voice_loop(conn, runtime)
    loop.agent.llm.turns = ["A long answer that gets interrupted partway through it."]
    loop.agent.llm.delay_s = 0.01

    assert await handle_utterance(conn, "tell me a story", runtime)
    assert await until(lambda: conn.tts.started > 0, timeout=5.0)
    assert await handle_barge_in(conn, "the person started speaking", runtime)

    assert conn.client_abort is True
    assert conn.cleared >= 1
    assert any('"state": "stop"' in payload for payload in conn.websocket.sent)


async def test_a_barge_in_on_an_unclaimed_session_is_a_no_op(wired):
    runtime, _ = wired
    conn = FakeSession()
    assert await handle_barge_in(conn, runtime=runtime) is False
    assert conn.cleared == 0


# -- lifetime ------------------------------------------------------------------------------------------


async def test_detaching_closes_the_loop(wired):
    runtime, _ = wired
    conn = FakeSession()
    await voice_loop(conn, runtime)

    await detach(conn)

    assert getattr(conn, LOOP_ATTR) is None
    assert runtime.voice(ROBOT_ID) is None


async def test_the_sink_drives_the_queue_and_never_re_enters_the_abort_handler():
    """``cancel`` does the three steps the inherited abort does, and calls nothing back."""
    conn = FakeSession()
    sink = ConnectionSpeechSink(conn)

    await sink.start(None)  # type: ignore[arg-type]
    await sink.say("hello ")
    await sink.say("there")
    await sink.finish()

    assert conn.tts.started == 1 and conn.tts.ended == 1
    assert conn.tts.drain() == ["hello ", "there"]

    await sink.cancel("barge-in")
    assert conn.client_abort is True
    assert conn.cleared == 1
    assert conn.speak_status_cleared == 1
