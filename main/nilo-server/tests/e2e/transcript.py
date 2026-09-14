"""Every state transition one scenario produced, and the report that prints them.

A scenario test asserts on a handful of facts. The transcript is the rest of the answer:
what actually happened, in order, with the correlation id that ties one link to the next.
It is written to ``tmp/e2e-report.md`` at the end of the run, which is what CI keeps as
the artifact for "produce a test report showing every state transition".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robot.events.types import RobotEvent

#: Fields worth printing for each kind of event, in the order a reader wants them. An
#: event type not listed here still appears; it just prints its identifiers only.
INTERESTING: dict[str, tuple[str, ...]] = {
    "RobotConnected": ("reconnect",),
    "RobotDisconnected": ("reason",),
    "CapabilitiesRefreshed": ("refreshed",),
    "UtteranceRecognized": ("text", "person_id"),
    "AudioStateChanged": ("previous", "state", "detail"),
    "AgentTurnStarted": ("utterance",),
    "AgentTurnCompleted": ("text", "tool_calls", "refusals", "fallback"),
    "AgentTurnFailed": ("error",),
    "ToolCallRefused": ("tool_name", "refused_by", "reason"),
    "ToolCallStarted": ("tool_name",),
    "ToolCallCompleted": ("tool_name", "duration_ms"),
    "ToolCallFailed": ("tool_name", "error"),
    "SpeechRequested": ("reason", "accepted", "rejected_because"),
    "SpeechStarted": ("reason",),
    "SpeechFinished": ("reason", "text", "interrupted"),
    "SpeechInterrupted": ("reason",),
    "MotionCompleted": ("action_id", "kind"),
    "MotionFailed": ("action_id", "kind", "reason", "detail"),
    "BehaviorSelected": ("behavior", "score", "reasons"),
    "BehaviorStarted": ("behavior",),
    "BehaviorCompleted": ("behavior", "outcome", "detail"),
    "BehaviorInterrupted": ("behavior", "reason"),
    "PersonDetected": ("track_id", "person_id", "x", "y"),
    "PersonLost": ("track_id", "reason"),
    "VisionFrameProcessed": ("detections", "tracks", "provider", "error"),
    "AnimationStarted": ("animation",),
    "AnimationFinished": ("animation", "outcome"),
    "EmergencyStopChanged": ("engaged", "reason"),
}

#: Events that say nothing a reader of a transition log wants: they arrive by the hundred
#: and carry no transition. Still counted, never printed.
NOISE: frozenset[str] = frozenset(
    {"TelemetryUpdated", "PoseUpdated", "SensorUpdated", "BatteryUpdated", "ToolDiscovered"}
)


@dataclass
class Line:
    """One recorded transition."""

    at_s: float
    event: str
    robot_id: str
    correlation_id: str
    detail: str

    def render(self, started: float) -> str:
        trace = self.correlation_id[:8] if self.correlation_id else "--------"
        return f"| {self.at_s - started:7.3f} | {trace} | {self.event:<24} | {self.detail} |"


@dataclass
class Transcript:
    """The state transitions of one scenario, newest last."""

    name: str
    description: str = ""
    lines: list[Line] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    notes: list[str] = field(default_factory=list)

    def __call__(self, event: RobotEvent) -> None:
        name = event.name
        self.counts[name] = self.counts.get(name, 0) + 1
        if name in NOISE:
            return
        self.lines.append(
            Line(
                at_s=time.monotonic(),
                event=name,
                robot_id=event.robot_id,
                correlation_id=event.correlation_id,
                detail=_detail(event),
            )
        )

    # -- what a test asks ------------------------------------------------------------------

    def note(self, text: str) -> None:
        """Record something the events cannot say: an assertion the scenario turns on."""
        self.notes.append(text)

    def of(self, *event_names: str) -> list[Line]:
        return [line for line in self.lines if line.event in event_names]

    def traces(self) -> dict[str, list[Line]]:
        """Every line, grouped by correlation id. What "can I follow one utterance?" means."""
        grouped: dict[str, list[Line]] = {}
        for line in self.lines:
            if line.correlation_id:
                grouped.setdefault(line.correlation_id, []).append(line)
        return grouped

    def render(self) -> str:
        body = [
            f"### {self.name}",
            "",
            self.description,
            "",
            "| t (s) | trace | event | detail |",
            "| ---: | :--- | :--- | :--- |",
        ]
        body += [line.render(self.started) for line in self.lines]
        body.append("")
        if self.notes:
            body += ["What this proves:", ""]
            body += [f"* {note}" for note in self.notes]
            body.append("")
        return "\n".join(body)


class Report:
    """Every scenario's transcript, collected across a run and written once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.transcripts: list[Transcript] = []

    def add(self, transcript: Transcript) -> None:
        self.transcripts.append(transcript)

    def write(self) -> Path:
        totals: dict[str, int] = {}
        for transcript in self.transcripts:
            for name, count in transcript.counts.items():
                totals[name] = totals.get(name, 0) + count
        header = [
            "# End-to-end scenario report",
            "",
            "Generated by `pytest tests/e2e`. Every line is one state transition the running",
            "system published on its own event bus; nothing here is written by an assertion.",
            "The `trace` column is the first eight characters of the correlation id, so the",
            "chain from an utterance to a device response is readable down a column.",
            "",
            f"Scenarios: **{len(self.transcripts)}**. Events observed: **{sum(totals.values())}**.",
            "",
            "| event | count |",
            "| :--- | ---: |",
        ]
        header += [f"| {name} | {count} |" for name, count in sorted(totals.items())]
        header.append("")
        body = "\n".join(header) + "\n" + "\n".join(transcript.render() for transcript in self.transcripts)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(body, encoding="utf-8")
        return self.path


def _detail(event: RobotEvent) -> str:
    names = INTERESTING.get(event.name, ())
    parts: list[str] = []
    for name in names:
        value = getattr(event, name, None)
        if value in (None, "", (), False):
            continue
        parts.append(f"{name}={_short(value)}")
    action = getattr(event, "action", None)
    if action is not None:
        parts.append(f"{action.action_type.value}/{action.status.value} source={action.source.value}")
        if action.device_action_id:
            # The id the *device* knows this motion by. Printing it on both sides is what
            # joins a `MotionCompleted` — which arrives on the read loop, outside any
            # trace — to the action it settles.
            parts.append(f"device_action_id={action.device_action_id}")
        if action.error is not None:
            parts.append(f"error={action.error.code}")
    return ", ".join(parts) or "-"


def _short(value: Any) -> str:
    text = str(value).replace("|", "/").replace("\n", " ")
    return text if len(text) <= 60 else text[:57] + "..."


__all__ = ["INTERESTING", "NOISE", "Line", "Report", "Transcript"]
