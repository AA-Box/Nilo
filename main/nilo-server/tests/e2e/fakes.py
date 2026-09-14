"""The four local fakes the end-to-end suite runs against. No network, no model, no clock.

Each one sits at a seam the production code already has, and each one is deterministic:
the same scenario produces the same transcript on every machine, which is the only way a
ten-scenario suite is worth putting in CI.
"""

from __future__ import annotations

import json
import queue
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: How a tool call is spelled on the wire by every provider the agent supports.
def tool_call(name: str, call_id: str = "", **arguments: Any) -> dict[str, Any]:
    return {
        "id": call_id or f"call-{name}",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@dataclass(frozen=True)
class Rule:
    """One utterance pattern, and what the model does about it."""

    pattern: str
    reply: str
    calls: tuple[dict[str, Any], ...] = ()

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE) is not None


#: The model's whole mind. Ordered: the first rule that matches wins.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        pattern=r"\b(closer|come here|come to me)\b",
        reply="Okay, coming closer.",
        calls=(tool_call("robot_move", distance_mm=300, speed_mmps=200),),
    ),
    Rule(
        pattern=r"\b(back up|move back|go back)\b",
        reply="Backing up.",
        calls=(tool_call("robot_move", distance_mm=-300, speed_mmps=200),),
    ),
    Rule(pattern=r"\b(stop|wait|hold on)\b", reply="Stopping.", calls=(tool_call("robot_stop"),)),
    Rule(
        pattern=r"\b(turn (around|left|right))\b",
        reply="Turning.",
        calls=(tool_call("robot_turn", angle_deg=90, speed_dps=90),),
    ),
    Rule(
        pattern=r"\b(look at me|look here|look up)\b",
        reply="Looking.",
        calls=(tool_call("robot_look_at", x_pct=50, y_pct=30),),
    ),
    Rule(pattern=r"\b(battery|charge left|power)\b", reply="Let me check.", calls=(tool_call("robot_get_battery"),)),
    Rule(pattern=r"\b(hello|hi|hey)\b", reply="Hello! Good to see you."),
    Rule(pattern=r"\b(drive (into|off)|jump|throw)\b", reply="I should not do that."),
)

#: The answer to anything no rule matches. A model that always has something to say is a
#: model a test cannot distinguish from a broken one, so this is deliberately recognizable.
DEFAULT_REPLY = "I heard you."


class RuleBasedLLM:
    """An :class:`~robot.agent.agent.LLMProvider` with a table instead of a network.

    One model round trip per call, exactly as a real provider streams one:
    ``(content, tool_calls)`` pairs from a generator, text a word at a time so a test can
    interrupt part-way through a sentence the way a person talking over the robot does.

    It answers the *last user or system message* in the dialogue, which is what makes a
    second round after a tool result produce the spoken reply rather than the tool call
    again — the same shape a real model's second round has.
    """

    def __init__(
        self,
        rules: Iterable[Rule] = DEFAULT_RULES,
        *,
        default_reply: str = DEFAULT_REPLY,
        delay_s: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        self.rules = tuple(rules)
        self.default_reply = default_reply
        self.delay_s = delay_s
        #: Raised instead of answering. What "the model is unavailable" looks like from
        #: inside the agent (scenario I).
        self.error = error
        self.calls = 0
        self.dialogues: list[list[dict[str, Any]]] = []
        self.offered: list[list[str]] = []

    def response_with_functions(
        self, session_id: str, dialogue: list[dict[str, Any]], functions: Any = None
    ) -> Any:
        self.calls += 1
        self.dialogues.append([dict(message) for message in dialogue])
        self.offered.append([entry["function"]["name"] for entry in functions or []])
        if self.error is not None:
            raise self.error
        prompt = _last_prompt(dialogue)
        rule = next((candidate for candidate in self.rules if candidate.matches(prompt)), None)
        answered_a_tool = any(message.get("role") == "tool" for message in dialogue)
        calls = () if (rule is None or answered_a_tool or not functions) else rule.calls
        reply = rule.reply if rule is not None else self.default_reply
        return self._emit(reply, calls)

    def _emit(self, reply: str, calls: tuple[dict[str, Any], ...]) -> Any:
        if calls:
            for call in calls:
                yield "", [call]
            return
        for word in _words(reply):
            if self.delay_s:
                time.sleep(self.delay_s)
            yield word, None


def _words(text: str) -> list[str]:
    """Split into streamable chunks that rejoin exactly, so a partial reply is readable."""
    return [piece for piece in re.split(r"(\s+)", text) if piece]


def _last_prompt(dialogue: list[dict[str, Any]]) -> str:
    for message in reversed(dialogue):
        if message.get("role") in ("user", "system") and message is not dialogue[0]:
            return str(message.get("content") or "")
    return ""


@dataclass
class SpokenStream:
    """One utterance the fake synthesizer was asked for."""

    sentence_id: str
    chunks: list[str] = field(default_factory=list)
    ended: bool = False

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class FakeTTS:
    """The ``conn.tts`` seam, with the synthesizer and the Opus encoder taken out.

    It implements exactly what :class:`~robot.voice.seam.ConnectionSpeechSink` calls, and
    it does the one thing the device can observe: it sends the ``tts`` state frames, so a
    simulated robot's ``state.speaking`` flips on and off from the server's own messages
    rather than from a test poking it.

    Reused rather than reimplemented where it matters: the queue is a real
    :class:`queue.Queue` holding the real ``TTSMessageDTO``, because the sink builds those
    and a fake that accepted anything would not catch the sink building them wrong.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.tts_text_queue: queue.Queue[Any] = queue.Queue()
        #: Never filled — there is no synthesizer here — but ``conn.clear_queues()`` drains
        #: it by name, so it has to exist.
        self.tts_audio_queue: queue.Queue[Any] = queue.Queue()
        self.streams: list[SpokenStream] = []
        self.stopped: list[str] = []
        self._current: SpokenStream | None = None

    # -- what the sink calls ---------------------------------------------------------------

    def tts_start(self, conn: Any, sentence_id: str | None = None) -> None:
        self._current = SpokenStream(sentence_id or getattr(conn, "sentence_id", ""))
        self.streams.append(self._current)
        conn.client_is_speaking = True
        self._send({"type": "tts", "state": "start", "session_id": getattr(conn, "session_id", "")})

    def tts_end(self, conn: Any, sentence_id: str | None = None) -> None:
        self._drain()
        if self._current is not None:
            self._current.ended = True
        self._current = None
        conn.client_is_speaking = False
        self._send({"type": "tts", "state": "stop", "session_id": getattr(conn, "session_id", "")})

    def store_tts_text(self, sentence_id: str, text: str) -> None:
        return None

    def tts_one_sentence(self, conn: Any, content_type: Any, content_detail: str = "", **_: Any) -> None:
        """The inherited "say this one line" path, used by wake words and plugins."""
        self.tts_start(conn)
        if self._current is not None:
            self._current.chunks.append(content_detail)
        self.tts_end(conn)

    async def close(self) -> None:
        """The inherited handler closes its synthesizer on teardown. Nothing to close here."""
        return None

    def clear_queues(self) -> None:
        while not self.tts_text_queue.empty():
            self.tts_text_queue.get_nowait()

    # -- what a test asks -------------------------------------------------------------------

    @property
    def spoken(self) -> list[str]:
        """Every utterance, complete or cut off, in order."""
        self._drain()
        return [stream.text for stream in self.streams]

    @property
    def speaking(self) -> bool:
        return self._current is not None

    def _drain(self) -> None:
        """Move whatever the sink queued into the current stream. The synthesizer's job."""
        while not self.tts_text_queue.empty():
            message = self.tts_text_queue.get_nowait()
            detail = getattr(message, "content_detail", None)
            if detail and self.streams:
                self.streams[-1].chunks.append(str(detail))

    def _send(self, payload: dict[str, Any]) -> None:
        """Tell the device. Scheduled on the loop, because the sink is not always on it."""
        import asyncio

        websocket = getattr(self.conn, "websocket", None)
        if websocket is None:
            return
        try:
            asyncio.get_running_loop().create_task(websocket.send(json.dumps(payload)))
        except RuntimeError:  # pragma: no cover - no loop: a unit test holding the fake
            pass


def install_fake_tts(conn: Any) -> FakeTTS:
    """Give one connection a fake synthesizer, and the state flags the sink reads."""
    fake = FakeTTS(conn)
    conn.tts = fake
    conn.client_abort = False
    conn.client_is_speaking = False
    if not getattr(conn, "sentence_id", None):
        conn.sentence_id = "e2e-sentence"
    return fake


__all__ = [
    "DEFAULT_REPLY",
    "DEFAULT_RULES",
    "FakeTTS",
    "Rule",
    "RuleBasedLLM",
    "SpokenStream",
    "install_fake_tts",
    "tool_call",
]
