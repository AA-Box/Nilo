"""The typed event vocabulary of the robot subsystem.

Events are frozen pydantic models with a common envelope (``event_id``, ``robot_id``,
``occurred_at``). Subscribers select by class, so a new event type is additive and a
subscriber of :class:`RobotEvent` sees everything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from robot.state.actions import ActionRecord
from robot.state.models import (
    DisconnectReason,
    RobotBatteryState,
    RobotCapabilities,
    RobotConnection,
    RobotIdentity,
    RobotPose,
    RobotSensorState,
    RobotTelemetry,
    RobotTool,
    utcnow,
)


class RobotEvent(BaseModel):
    """Envelope every robot event carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = Field(default_factory=utcnow)
    robot_id: str

    @property
    def name(self) -> str:
        return type(self).__name__


class RobotConnected(RobotEvent):
    """A robot session was registered. ``reconnect`` is true for a returning device."""

    identity: RobotIdentity
    connection: RobotConnection
    reconnect: bool = False


class RobotDisconnected(RobotEvent):
    session_id: str
    reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED


class TelemetryUpdated(RobotEvent):
    telemetry: RobotTelemetry
    changed: tuple[str, ...] = ()


class BatteryUpdated(RobotEvent):
    battery: RobotBatteryState


class PoseUpdated(RobotEvent):
    pose: RobotPose


class SensorUpdated(RobotEvent):
    sensors: RobotSensorState


class MotionCompleted(RobotEvent):
    """A motion the robot accepted finished on its own. Reported by the device, not inferred."""

    action_id: str
    kind: str = "move"
    pose: RobotPose | None = None


class MotionFailed(RobotEvent):
    """A motion ended without finishing. ``reason`` is the device's word for why."""

    action_id: str
    kind: str = "move"
    reason: str = "unknown"
    detail: str = ""


class ToolDiscovered(RobotEvent):
    tool: RobotTool


class CapabilitiesRefreshed(RobotEvent):
    """Discovery finished (first connection or a refresh after a reconnect)."""

    capabilities: RobotCapabilities
    refreshed: bool = False


class ToolCallStarted(RobotEvent):
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallCompleted(RobotEvent):
    call_id: str
    tool_name: str
    result: str = ""
    duration_ms: float = 0.0


class ToolCallFailed(RobotEvent):
    call_id: str
    tool_name: str
    error: str
    duration_ms: float = 0.0


class ActionSubmitted(RobotEvent):
    """An action was created and handed to the executor. Published even if it is rejected
    in the same breath, so the audit trail shows what was *asked for*, not only what ran."""

    action: ActionRecord


class ActionStarted(RobotEvent):
    """An action passed safety, took its resource claims, and is being dispatched."""

    action: ActionRecord


class ActionFinished(RobotEvent):
    """An action reached a terminal state — any of them.

    One event for every ending, because a subscriber that cares about "it is over" should
    not have to subscribe to five types to learn it. Which ending it was is
    ``event.action.status``, and why is ``event.action.error``.
    """

    action: ActionRecord

    @property
    def succeeded(self) -> bool:
        return self.action.succeeded


class BehaviorEvaluated(RobotEvent):
    """One scoring pass of the behaviour engine, in full.

    The debug event: every candidate with its score and the reasons it gave, including
    the ones that were filtered out and why. It is what
    ``python -m robot.behavior explain`` prints and what a management API serves when
    somebody asks why the robot is doing what it is doing.

    Payloads are primitives rather than behaviour objects on purpose — ``robot/events``
    sits below ``robot/behavior`` in the layering and may not import it.
    """

    tick: int = 0
    mode: str = "normal"
    #: ``name -> score``, every candidate that was scored, highest first.
    scores: tuple[tuple[str, float], ...] = ()
    #: ``name -> reason`` for candidates that never reached scoring.
    rejected: tuple[tuple[str, str], ...] = ()
    #: ``name -> (reason, ...)`` for the reasons each candidate recorded.
    reasons: tuple[tuple[str, tuple[str, ...]], ...] = ()
    selected: str | None = None


class BehaviorSelected(RobotEvent):
    """The engine chose a behaviour. Not yet that it started — safety may still refuse."""

    behavior: str
    score: float = 0.0
    category: str = ""
    priority: int = 0
    #: The runners-up, highest first. Two or three is enough to explain a decision.
    alternatives: tuple[tuple[str, float], ...] = ()
    reasons: tuple[str, ...] = ()
    preempted: str | None = None


class BehaviorStarted(RobotEvent):
    behavior: str
    score: float = 0.0
    resources: tuple[str, ...] = ()


class BehaviorCompleted(RobotEvent):
    behavior: str
    outcome: str = "completed"
    detail: str = ""
    duration_s: float = 0.0


class BehaviorInterrupted(RobotEvent):
    """A running behaviour was stopped before it finished. ``by`` names what took over."""

    behavior: str
    reason: str = "preempted"
    by: str | None = None
    duration_s: float = 0.0


class PersonDetected(RobotEvent):
    """A person track was confirmed. ``track_id`` is stable until the person is lost.

    Vision events carry the normalized image point rather than a pixel box, and an id
    rather than an embedding: the coordinates have to survive a change of camera, and the
    biometric data has to stay in the face registry (docs/robot-vision.md).
    """

    track_id: str
    x: float = 0.5
    y: float = 0.5
    area: float = 0.0
    confidence: float = 1.0
    person_id: str | None = None
    display_name: str = ""


class PersonLost(RobotEvent):
    """A person track went unseen for long enough to be dropped."""

    track_id: str
    person_id: str | None = None
    reason: str = "timeout"
    frames_missing: int = 0


class FaceDetected(RobotEvent):
    track_id: str
    x: float = 0.5
    y: float = 0.5
    confidence: float = 1.0


class KnownPersonRecognized(RobotEvent):
    """A face was matched to somebody the registry already knows."""

    track_id: str
    person_id: str
    display_name: str = ""
    confidence: float = 0.0
    embedding_ref: str | None = None


class UnknownPersonDetected(RobotEvent):
    """A face was found and not recognized. Not an error: most faces are strangers."""

    track_id: str
    x: float = 0.5
    y: float = 0.5
    confidence: float = 1.0


class ObjectDetected(RobotEvent):
    track_id: str
    label: str = "object"
    x: float = 0.5
    y: float = 0.5
    area: float = 0.0
    confidence: float = 1.0


class ObjectLost(RobotEvent):
    track_id: str
    label: str = "object"
    reason: str = "timeout"


class VisionFrameProcessed(RobotEvent):
    """One pass of the pipeline, with its latency. The metrics event.

    Published for every frame, successful or not, because "perception stopped answering"
    and "perception answered with nothing" look identical from the world model.
    """

    latency_ms: float = 0.0
    detections: int = 0
    tracks: int = 0
    provider: str = ""
    error: str = ""


class AnimationStarted(RobotEvent):
    """An animation began playing, and what it took ownership of while it does."""

    animation: str
    priority: int = 0
    loop: bool = False
    resources: tuple[str, ...] = ()


class AnimationFinished(RobotEvent):
    animation: str
    outcome: str = "completed"
    detail: str = ""
    passes: int = 1
    duration_s: float = 0.0


class AnimationCancelled(RobotEvent):
    """An animation stopped early, or never started. ``reason`` says which and why —
    preempted, restarted, closed, or refused because something else held the head."""

    animation: str
    reason: str = "cancelled"


class EmergencyStopChanged(RobotEvent):
    """The emergency-stop latch was engaged or cleared for one robot."""

    engaged: bool
    reason: str = ""
    engaged_by: str = "system"


# -- the agent ---------------------------------------------------------------------------------
#
# What the model was asked, what it answered, and what it was not allowed to do. The
# conversation is observable from the outside for the same reason actions are: an
# operator asking "why did the robot drive off?" needs the turn that caused it, not a log
# line. Utterances and replies are carried because they are the event; a deployment that
# must not keep them turns the subscriber off rather than the events.


class AgentTurnStarted(RobotEvent):
    """A turn began: somebody spoke, or a behaviour asked the robot to say something."""

    turn_id: str
    origin: str = "user"
    person_id: str | None = None
    utterance: str = ""


class AgentTurnCompleted(RobotEvent):
    """A turn settled. ``fallback`` names why the model was not used, when it was not."""

    turn_id: str
    text: str = ""
    tool_calls: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    fallback: str = ""


class AgentTurnFailed(RobotEvent):
    turn_id: str
    error: str


class ToolCallRefused(RobotEvent):
    """A tool the model asked for was not run, and what refused it.

    ``refused_by`` is ``permission_policy``, ``argument_validation``, ``timeout``, or
    ``safety:<typed reason>`` — enough to tell a policy problem from a hazard without
    reading the message.
    """

    turn_id: str = ""
    tool_name: str
    refused_by: str = ""
    reason: str = ""


class SpeechRequested(RobotEvent):
    """Something asked the robot to speak, and what the arbiter did about it."""

    intent_id: str
    reason: str
    priority: int = 0
    accepted: bool = True
    rejected_because: str = ""
    interrupted: str | None = None


class SpeechInterrupted(RobotEvent):
    """Speech stopped before it finished. The barge-in event."""

    intent_id: str = ""
    reason: str = "interrupted"


# -- the voice loop ------------------------------------------------------------------------------
#
# One answer to "is this robot talking?". The inherited session has several — client
# flags, the TTS queue's own state, and whatever the device believes — and a barge-in
# that reads a different one from the one the speaker wrote is a robot that talks over
# the person who interrupted it.


class AudioStateChanged(RobotEvent):
    """The voice loop moved between IDLE, LISTENING, THINKING, SPEAKING and INTERRUPTED."""

    previous: str = "idle"
    state: str = "idle"
    detail: str = ""


class UtteranceRecognized(RobotEvent):
    """Speech recognition produced a final result, and who it was attributed to."""

    text: str
    person_id: str | None = None
    speaker: str = ""


class SpeechStarted(RobotEvent):
    """Audio began streaming to the robot's speaker."""

    intent_id: str = ""
    reason: str = ""
    priority: int = 0
    text: str = ""


class SpeechFinished(RobotEvent):
    """Audio stopped. ``interrupted`` says whether it reached the end of the sentence."""

    intent_id: str = ""
    reason: str = ""
    text: str = ""
    interrupted: bool = False


__all__ = [
    "ActionFinished",
    "AgentTurnCompleted",
    "AgentTurnFailed",
    "AgentTurnStarted",
    "AudioStateChanged",
    "AnimationCancelled",
    "AnimationFinished",
    "AnimationStarted",
    "BehaviorCompleted",
    "BehaviorEvaluated",
    "BehaviorInterrupted",
    "BehaviorSelected",
    "BehaviorStarted",
    "ActionStarted",
    "ActionSubmitted",
    "BatteryUpdated",
    "CapabilitiesRefreshed",
    "EmergencyStopChanged",
    "MotionCompleted",
    "MotionFailed",
    "PoseUpdated",
    "RobotConnected",
    "RobotDisconnected",
    "RobotEvent",
    "SensorUpdated",
    "SpeechFinished",
    "SpeechInterrupted",
    "SpeechRequested",
    "SpeechStarted",
    "TelemetryUpdated",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallRefused",
    "ToolCallStarted",
    "ToolDiscovered",
    "UtteranceRecognized",
    "FaceDetected",
    "KnownPersonRecognized",
    "ObjectDetected",
    "ObjectLost",
    "PersonDetected",
    "PersonLost",
    "UnknownPersonDetected",
    "VisionFrameProcessed",
]
