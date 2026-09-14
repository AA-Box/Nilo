"""Metrics and the traceable log line, from the events the subsystem already publishes.

    observer = RobotObserver(runtime.metrics)
    observer.attach(runtime.events)

One subscriber, one file. Nothing else in ``robot/`` records a metric or writes a trace
line, which is the point: instrumentation scattered across thirty modules is thirty places
to forget, thirty imports of a metrics object, and thirty chances to slow down an audio
path. The event vocabulary (``robot/events/types.py``) already describes everything worth
measuring, so measuring is a fold over it.

What this produces is two things from one stream:

* **Metrics.** Counters, gauges and latency histograms on :class:`~robot.metrics.MetricRegistry`,
  scraped at ``/metrics`` on the management API.
* **The trace.** One ``robot.trace`` log line per event that carries a correlation id, in
  a fixed ``key=value`` shape, so the whole chain

      utterance -> model turn -> tool call -> action -> device call -> completion

  is ``grep correlation_id=<id>`` and nothing more (docs/observability.md).

Latency that the events do not carry directly is derived here by pairing a start with an
end — a model turn, a speech stream — and the pairing tables are bounded, because a start
whose end never arrives must cost a fixed amount of memory rather than a growing one.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from robot.events.types import (
    ActionFinished,
    AgentTurnCompleted,
    AgentTurnFailed,
    AgentTurnStarted,
    AudioStateChanged,
    BehaviorSelected,
    CapabilitiesRefreshed,
    MotionCompleted,
    MotionFailed,
    RobotConnected,
    RobotDisconnected,
    RobotEvent,
    SpeechFinished,
    SpeechStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRefused,
    UtteranceRecognized,
    VisionFrameProcessed,
)
from robot.metrics import MetricRegistry

logger = logging.getLogger("robot.trace")

#: How many unfinished starts to remember while waiting for their end. A model turn or a
#: speech stream that never completes leaks nothing: the oldest entry is evicted.
PENDING_LIMIT = 256

#: Metric names, in one place so the documentation and the tests can name them.
ROBOTS_CONNECTED = "nilo_robot_connected"
SESSIONS_TOTAL = "nilo_robot_sessions_total"
DISCONNECTS_TOTAL = "nilo_robot_disconnects_total"
DISCOVERY_TOTAL = "nilo_robot_capability_discoveries_total"
TOOL_CALLS_TOTAL = "nilo_robot_tool_calls_total"
TOOL_ERRORS_TOTAL = "nilo_robot_tool_errors_total"
TOOL_REFUSALS_TOTAL = "nilo_robot_tool_refusals_total"
TOOL_LATENCY = "nilo_robot_tool_latency_seconds"
ACTIONS_TOTAL = "nilo_robot_actions_total"
ACTION_LATENCY = "nilo_robot_action_latency_seconds"
MOTIONS_TOTAL = "nilo_robot_motions_total"
LLM_TURNS_TOTAL = "nilo_robot_llm_turns_total"
LLM_LATENCY = "nilo_robot_llm_latency_seconds"
ASR_UTTERANCES_TOTAL = "nilo_robot_asr_utterances_total"
ASR_LATENCY = "nilo_robot_asr_latency_seconds"
TTS_STREAMS_TOTAL = "nilo_robot_tts_streams_total"
TTS_LATENCY = "nilo_robot_tts_latency_seconds"
TTS_TIME_TO_FIRST_AUDIO = "nilo_robot_tts_time_to_first_audio_seconds"
VISION_FRAMES_TOTAL = "nilo_robot_vision_frames_total"
VISION_LATENCY = "nilo_robot_vision_latency_seconds"
BEHAVIOR_DECISIONS_TOTAL = "nilo_robot_behavior_decisions_total"
EVENTS_TOTAL = "nilo_robot_events_total"

_HELP: dict[str, str] = {
    ROBOTS_CONNECTED: "Robots with a live session right now.",
    SESSIONS_TOTAL: "Robot WebSocket sessions accepted since start.",
    DISCONNECTS_TOTAL: "Robot sessions that ended, by reason.",
    DISCOVERY_TOTAL: "Capability discoveries that finished, by whether the device speaks MCP.",
    TOOL_CALLS_TOTAL: "Device tool calls that completed, by tool.",
    TOOL_ERRORS_TOTAL: "Device tool calls that failed, by tool.",
    TOOL_REFUSALS_TOTAL: "Tool calls a permission or safety layer refused, by tool.",
    TOOL_LATENCY: "Device tool call round trip, in seconds.",
    ACTIONS_TOTAL: "Actions that reached a terminal state, by type and status.",
    ACTION_LATENCY: "Dispatch to terminal state for one action, in seconds.",
    MOTIONS_TOTAL: "Motions the device reported the end of, by outcome.",
    LLM_TURNS_TOTAL: "Agent turns that finished, by outcome.",
    LLM_LATENCY: "Agent turn from start to answer, in seconds.",
    ASR_UTTERANCES_TOTAL: "Utterances the recognizer produced.",
    ASR_LATENCY: "Speech recognition, in seconds. Only for sessions that report it.",
    TTS_STREAMS_TOTAL: "Speech streams that ended, by whether they were interrupted.",
    TTS_LATENCY: "One speech stream from first audio to the end, in seconds.",
    TTS_TIME_TO_FIRST_AUDIO: "Thinking to the first audio chunk, in seconds.",
    VISION_FRAMES_TOTAL: "Camera frames the pipeline processed, by outcome.",
    VISION_LATENCY: "One vision frame through capture, decode and detection, in seconds.",
    BEHAVIOR_DECISIONS_TOTAL: "Behaviours the scheduler selected, by behaviour.",
    EVENTS_TOTAL: "Robot events published, by type.",
}


class RobotObserver:
    """Folds the event stream into metrics and one trace line per event."""

    def __init__(self, metrics: MetricRegistry | None = None, *, trace: bool = True) -> None:
        self.metrics = metrics if metrics is not None else MetricRegistry()
        self.trace = trace
        self._subscription: Any = None
        self._connected: set[str] = set()
        # start timestamps waiting for their end, newest last and bounded
        self._turns: OrderedDict[str, float] = OrderedDict()
        self._speech: OrderedDict[str, float] = OrderedDict()
        self._thinking: OrderedDict[str, float] = OrderedDict()
        for name, help_text in _HELP.items():
            self.metrics.describe(name, help_text)

    # -- lifecycle ------------------------------------------------------------------------

    def attach(self, events: Any) -> Any:
        """Subscribe to every robot event. Returns the subscription."""
        if self._subscription is None:
            self._subscription = events.subscribe(RobotEvent, self.observe)
        return self._subscription

    def detach(self, events: Any) -> None:
        """Unsubscribe. Idempotent, and it never raises: teardown must not fail on this."""
        subscription, self._subscription = self._subscription, None
        if subscription is None:
            return
        try:
            events.unsubscribe(subscription)
        except Exception as exc:  # pragma: no cover - a closed bus is a normal teardown order
            logger.debug("detaching the observer failed: %s", exc)

    # -- the fold --------------------------------------------------------------------------

    def observe(self, event: RobotEvent) -> None:
        """Record one event. Never raises — a metric is not worth losing an event over."""
        try:
            self.metrics.counter(EVENTS_TOTAL, event=event.name).inc()
            self._record(event)
        except Exception:  # pragma: no cover - defensive: the bus logs and carries on
            logger.exception("recording %s failed", event.name)
        if self.trace:
            self._log(event)

    def _record(self, event: RobotEvent) -> None:
        robot_id = event.robot_id
        seconds = _elapsed_seconds(event)

        if isinstance(event, RobotConnected):
            self._connected.add(robot_id)
            self.metrics.counter(SESSIONS_TOTAL, reconnect=str(event.reconnect).lower()).inc()
            self.metrics.gauge(ROBOTS_CONNECTED).set(len(self._connected))
        elif isinstance(event, RobotDisconnected):
            self._connected.discard(robot_id)
            self.metrics.counter(DISCONNECTS_TOTAL, reason=event.reason.value).inc()
            self.metrics.gauge(ROBOTS_CONNECTED).set(len(self._connected))
        elif isinstance(event, CapabilitiesRefreshed):
            self.metrics.counter(DISCOVERY_TOTAL, mcp=str(event.capabilities.mcp).lower()).inc()

        elif isinstance(event, ToolCallCompleted):
            self.metrics.counter(TOOL_CALLS_TOTAL, tool=event.tool_name).inc()
            self.metrics.histogram(TOOL_LATENCY, tool=event.tool_name).observe(seconds)
        elif isinstance(event, ToolCallFailed):
            self.metrics.counter(TOOL_ERRORS_TOTAL, tool=event.tool_name).inc()
            self.metrics.histogram(TOOL_LATENCY, tool=event.tool_name).observe(seconds)
        elif isinstance(event, ToolCallRefused):
            self.metrics.counter(
                TOOL_REFUSALS_TOTAL, tool=event.tool_name, refused_by=event.refused_by or "unknown"
            ).inc()

        elif isinstance(event, ActionFinished):
            record = event.action
            self.metrics.counter(
                ACTIONS_TOTAL, action_type=record.action_type.value, status=record.status.value
            ).inc()
            duration = record.duration_s
            if duration is not None:
                self.metrics.histogram(ACTION_LATENCY, action_type=record.action_type.value).observe(duration)
        elif isinstance(event, MotionCompleted):
            self.metrics.counter(MOTIONS_TOTAL, kind=event.kind, outcome="completed").inc()
        elif isinstance(event, MotionFailed):
            self.metrics.counter(MOTIONS_TOTAL, kind=event.kind, outcome=event.reason or "failed").inc()

        elif isinstance(event, AgentTurnStarted):
            _remember(self._turns, event.turn_id, _seconds(event))
        elif isinstance(event, AgentTurnCompleted):
            self._finish_turn(event.turn_id, event, "fallback" if event.fallback else "answered")
        elif isinstance(event, AgentTurnFailed):
            self._finish_turn(event.turn_id, event, "failed")

        elif isinstance(event, UtteranceRecognized):
            self.metrics.counter(ASR_UTTERANCES_TOTAL).inc()
            if event.latency_ms > 0:
                self.metrics.histogram(ASR_LATENCY).observe(event.latency_ms / 1000.0)
        elif isinstance(event, SpeechStarted):
            _remember(self._speech, event.intent_id, _seconds(event))
        elif isinstance(event, SpeechFinished):
            started = self._speech.pop(event.intent_id, None)
            self.metrics.counter(
                TTS_STREAMS_TOTAL, outcome="interrupted" if event.interrupted else "completed"
            ).inc()
            if started is not None:
                self.metrics.histogram(TTS_LATENCY).observe(_seconds(event) - started)
        elif isinstance(event, AudioStateChanged):
            self._audio_state(event)

        elif isinstance(event, VisionFrameProcessed):
            self.metrics.counter(
                VISION_FRAMES_TOTAL, outcome="error" if event.error else "ok", provider=event.provider or "none"
            ).inc()
            self.metrics.histogram(VISION_LATENCY).observe(event.latency_ms / 1000.0)

        elif isinstance(event, BehaviorSelected):
            self.metrics.counter(BEHAVIOR_DECISIONS_TOTAL, behavior=event.behavior).inc()

    def _finish_turn(self, turn_id: str, event: RobotEvent, outcome: str) -> None:
        started = self._turns.pop(turn_id, None)
        self.metrics.counter(LLM_TURNS_TOTAL, outcome=outcome).inc()
        if started is not None:
            self.metrics.histogram(LLM_LATENCY).observe(_seconds(event) - started)

    def _audio_state(self, event: AudioStateChanged) -> None:
        """Thinking to the first audio chunk: what a person experiences as the pause."""
        key = event.robot_id
        if event.state == "thinking":
            _remember(self._thinking, key, _seconds(event))
        elif event.state == "speaking":
            started = self._thinking.pop(key, None)
            if started is not None:
                self.metrics.histogram(TTS_TIME_TO_FIRST_AUDIO).observe(_seconds(event) - started)
        elif event.state in ("idle", "interrupted"):
            self._thinking.pop(key, None)

    # -- the trace -------------------------------------------------------------------------

    def _log(self, event: RobotEvent) -> None:
        """One line per correlated event, in a shape ``grep`` and ``cut`` both handle."""
        if not event.correlation_id:
            return
        fields = [
            f"correlation_id={event.correlation_id}",
            f"event={event.name}",
            f"robot_id={event.robot_id or '-'}",
        ]
        fields += [f"{name}={_field(value)}" for name, value in _detail(event)]
        logger.info(" ".join(fields))


def _detail(event: RobotEvent) -> list[tuple[str, Any]]:
    """The identifiers that link one event to the next link in the chain.

    Deliberately a fixed list of names rather than the whole model: a trace line has to
    stay one line, and dumping an event that carries a base64 camera frame into the log
    is how a disk fills up.
    """
    names = (
        "turn_id",
        "call_id",
        "action_id",
        "intent_id",
        "tool_name",
        "behavior",
        "session_id",
        "person_id",
        "track_id",
        "reason",
        "outcome",
        "status",
        "error",
    )
    found = [(name, getattr(event, name)) for name in names if getattr(event, name, None)]
    action = getattr(event, "action", None)
    if action is not None:
        found += [
            ("action_id", action.action_id),
            ("action_type", action.action_type.value),
            ("status", action.status.value),
            ("source", action.source.value),
        ]
        if action.device_action_id:
            # What the device calls this motion. A `MotionCompleted` arrives on the read
            # loop with no trace of its own, and this is the key that joins the two lines.
            found.append(("device_action_id", action.device_action_id))
    return found


def _field(value: Any) -> str:
    text = str(value).replace("\n", " ").strip()
    if len(text) > 120:
        text = text[:117] + "..."
    return text.replace(" ", "_") if " " in text else text or "-"


def _seconds(event: RobotEvent) -> float:
    return event.occurred_at.timestamp()


def _elapsed_seconds(event: RobotEvent) -> float:
    return float(getattr(event, "duration_ms", 0.0)) / 1000.0


def _remember(table: OrderedDict[str, float], key: str, value: float) -> None:
    """Record a start, and evict the oldest one if the table has grown too large."""
    table[key] = value
    table.move_to_end(key)
    while len(table) > PENDING_LIMIT:
        table.popitem(last=False)


__all__ = [
    "ACTIONS_TOTAL",
    "ACTION_LATENCY",
    "ASR_LATENCY",
    "ASR_UTTERANCES_TOTAL",
    "BEHAVIOR_DECISIONS_TOTAL",
    "DISCONNECTS_TOTAL",
    "DISCOVERY_TOTAL",
    "EVENTS_TOTAL",
    "LLM_LATENCY",
    "LLM_TURNS_TOTAL",
    "MOTIONS_TOTAL",
    "PENDING_LIMIT",
    "ROBOTS_CONNECTED",
    "RobotObserver",
    "SESSIONS_TOTAL",
    "TOOL_CALLS_TOTAL",
    "TOOL_ERRORS_TOTAL",
    "TOOL_LATENCY",
    "TOOL_REFUSALS_TOTAL",
    "TTS_LATENCY",
    "TTS_STREAMS_TOTAL",
    "TTS_TIME_TO_FIRST_AUDIO",
    "VISION_FRAMES_TOTAL",
    "VISION_LATENCY",
]
