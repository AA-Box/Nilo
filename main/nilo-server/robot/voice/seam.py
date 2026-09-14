"""Where the voice loop attaches to the inherited session, in two calls.

    from robot.voice import handle_barge_in, handle_utterance

    if await handle_utterance(conn, text):   # claims robot sessions; never raises
        return
    await handle_barge_in(conn)              # from the inherited abort handler

Both never raise and both return ``False`` for a session they do not claim, so the
inherited path is unchanged for every device that is not a robot. That matters: the
session layer registers *any* device as a robot, and a smart speaker is not a robot. The
gate is capability-based — a session is claimed only when the device published the motion
tools the action layer dispatches to.

The sink is the inherited text-to-speech pipeline, driven through its own queue rather
than reimplemented: ``tts_start`` puts the FIRST marker, each chunk goes in as a MIDDLE
text message, and ``tts_end`` puts the LAST one. Sentence splitting, streaming synthesis,
Opus encoding and the send loop are all upstream code doing what it already does.

``core`` is imported inside functions, like everywhere else under ``robot/``
(docs/robot-architecture.md R12).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

from robot.actions.model import SPEC_TYPES
from robot.agent.speech import SpeakIntent
from robot.session import ROBOT_ATTR
from robot.voice.loop import VoiceLoop

logger = logging.getLogger(__name__)

#: The attribute the voice loop is cached under on a ConnectionHandler. Namespaced, like
#: every other attribute robot code adds to the inherited handler.
LOOP_ATTR = "nilo_robot_voice"

#: Where the inherited ASR path leaves how long recognition took, in milliseconds. Set by
#: one line in ``core/providers/asr/base.py`` and read here; absent on a session whose
#: text did not come from the recognizer, which is why the default is zero and zero means
#: "not measured" (docs/upstream.md).
ASR_LATENCY_ATTR = "nilo_asr_latency_ms"

#: A device that publishes any of these is a robot, and its session is claimed. A device
#: that publishes none of them is a speaker with a microphone, and the inherited chat path
#: is exactly right for it.
ROBOT_TOOL_NAMES: frozenset[str] = frozenset(spec.tool_name for spec in SPEC_TYPES.values())


class ConnectionSpeechSink:
    """The inherited text-to-speech pipeline, as a :class:`~robot.voice.loop.SpeechSink`."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def start(self, intent: SpeakIntent) -> None:
        conn = self.conn
        conn.client_abort = False
        conn.sentence_id = uuid4().hex
        conn.tts.tts_start(conn)

    async def say(self, text: str) -> None:
        from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO  # noqa: PLC0415

        conn = self.conn
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.MIDDLE,
                content_type=ContentType.TEXT,
                content_detail=text,
            )
        )

    async def finish(self) -> None:
        conn = self.conn
        conn.tts.store_tts_text(conn.sentence_id, "")
        conn.tts.tts_end(conn)

    async def cancel(self, reason: str) -> None:
        """Stop the stream in flight. Idempotent, and it never re-enters the abort handler.

        This is deliberately the same three steps the inherited abort performs and nothing
        more: setting the flag, dropping the queued audio, and telling the device to stop
        playing. Calling ``handleAbortMessage`` from here would recurse, because that is
        one of the two places this seam is called *from*.
        """
        conn = self.conn
        conn.client_abort = True
        try:
            conn.clear_queues()
            await conn.websocket.send(
                json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
            )
            conn.clearSpeakStatus()
        except Exception as exc:
            logger.warning("robot voice: cancelling the speech stream failed: %s", exc)


async def is_robot_session(conn: Any, runtime: Any = None) -> bool:
    """Whether this session belongs to a device that is actually a robot."""
    session = getattr(conn, ROBOT_ATTR, None)
    robot_id = getattr(session, "robot_id", None)
    if not robot_id:
        return False
    from robot.runtime import get_runtime  # noqa: PLC0415

    active = runtime or get_runtime()
    capabilities = await active.capabilities.get(robot_id)
    if capabilities is None:
        return False
    return bool(set(capabilities.tool_names) & ROBOT_TOOL_NAMES)


async def voice_loop(conn: Any, runtime: Any = None) -> VoiceLoop | None:
    """The voice loop for this session, built on first use. ``None`` for a non-robot.

    Cached on the connection rather than in a module dictionary: the inherited handler
    owns the lifetime, two sessions for one device are normal during a reconnect, and a
    process-wide map keyed by robot id is a leak with a race in it.
    """
    cached = getattr(conn, LOOP_ATTR, None)
    if isinstance(cached, VoiceLoop):
        return cached
    if not await is_robot_session(conn, runtime):
        return None
    from robot.runtime import get_runtime  # noqa: PLC0415

    active = runtime or get_runtime()
    session = getattr(conn, ROBOT_ATTR, None)
    robot_id = str(getattr(session, "robot_id", ""))
    agent = await active.agent(robot_id)
    if agent.llm is None:
        # Take the provider this session already has. The inherited handler built (or was
        # handed) one for every connection, and it is the model this device would have
        # been answered by anyway — including the private one a device with its own
        # configuration gets. Without this the runtime's provider is never set by anything
        # in the server, and the whole voice loop is unreachable in production.
        agent.llm = getattr(conn, "llm", None)
    if agent.llm is None:
        # Still nothing: the inherited chat path has no provider either, and a fallback
        # line is a worse answer than the one the session was already going to give.
        logger.info("robot %s: no LLM provider on this session; leaving the inherited chat path", robot_id)
        return None
    built = VoiceLoop(
        robot_id,
        agent,
        ConnectionSpeechSink(conn),
        events=active.events,
        animations=active.animations(robot_id),
        arbiter=active.speech(robot_id),
    )
    setattr(conn, LOOP_ATTR, built)
    active.register_voice(robot_id, built)
    logger.info("robot %s: the voice loop is driving this session", robot_id)
    return built


async def handle_utterance(conn: Any, text: str, runtime: Any = None) -> bool:
    """Answer through the robot agent. Returns whether this seam claimed the utterance.

    The turn runs in its own task: the caller is the inherited message handler, which the
    read loop awaits inline, so anything that waits here stalls ingestion of every later
    frame — audio included (docs/robot-architecture.md R3).
    """
    try:
        loop = await voice_loop(conn, runtime)
        if loop is None:
            return False
        speaker = str(getattr(conn, "current_speaker", "") or "")
        person_id = _person_id(speaker)
        task = asyncio.create_task(
            loop.on_utterance(
                text,
                person_id=person_id,
                speaker=speaker,
                asr_latency_ms=_asr_latency_ms(conn),
            ),
            name=f"robot-voice-turn-{loop.robot_id}",
        )
        _TURNS.add(task)
        task.add_done_callback(_TURNS.discard)
        return True
    except Exception as exc:
        logger.warning("robot voice: could not claim this utterance: %s", exc)
        return False


async def handle_barge_in(conn: Any, reason: str = "the person started speaking", runtime: Any = None) -> bool:
    """Somebody talked over the robot. Returns whether anything was interrupted.

    Called from the inherited abort handler, which has already cleared the audio queues.
    Safe when the robot is silent, which is what makes it safe to wire in unconditionally.
    """
    try:
        loop = getattr(conn, LOOP_ATTR, None)
        if not isinstance(loop, VoiceLoop):
            return False
        return await loop.barge_in(reason)
    except Exception as exc:
        logger.warning("robot voice: handling a barge-in failed: %s", exc)
        return False


async def detach(conn: Any) -> None:
    """Close the voice loop bound to this session. Idempotent, and never raises."""
    loop = getattr(conn, LOOP_ATTR, None)
    setattr(conn, LOOP_ATTR, None)
    if isinstance(loop, VoiceLoop):
        try:
            from robot.runtime import get_runtime  # noqa: PLC0415

            get_runtime().register_voice(loop.robot_id, None)
            await loop.aclose()
        except Exception as exc:
            logger.warning("robot voice: closing the voice loop failed: %s", exc)


#: In-flight turns, held so the garbage collector does not drop a running task.
_TURNS: set[asyncio.Task[Any]] = set()


def _asr_latency_ms(conn: Any) -> float:
    """How long recognition took, or ``0.0`` when this session does not measure it."""
    try:
        return max(0.0, float(getattr(conn, ASR_LATENCY_ATTR, 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _person_id(speaker: str) -> str | None:
    """A person id from whatever the voiceprint layer attributed the utterance to.

    Lowercased and stripped, because the same person is the same person whether the
    provider spelled them ``Ahmad`` or ``ahmad``, and memory is keyed on this.
    """
    cleaned = speaker.strip().lower()
    return cleaned or None


__all__ = [
    "ASR_LATENCY_ATTR",
    "LOOP_ATTR",
    "ROBOT_TOOL_NAMES",
    "ConnectionSpeechSink",
    "detach",
    "handle_barge_in",
    "handle_utterance",
    "is_robot_session",
    "voice_loop",
]
