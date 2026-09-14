"""The voice and interaction loop: microphone to speaker, with a robot in the middle.

    from robot.voice import AudioState, VoiceLoop, handle_barge_in, handle_utterance

    loop = VoiceLoop("nilo-sim-01", agent, sink, events=bus)
    await loop.on_utterance("come closer", person_id="ahmad")

    microphone -> VAD -> ASR -> person -> RobotAgent -> tools -> model -> TTS -> speaker

The ends of that path are inherited and unchanged: Silero VAD, the streaming ASR
providers, the sentence-chunked TTS queue, the Opus encoder and the device WebSocket. This
package is the part in the middle that did not exist — who is allowed to talk, what the
face does while they do, and what happens when somebody talks over the robot.

``state``       five audio states and the checked transitions between them
``expression``  the face, following the conversation, sparingly
``loop``        the turn: think, stream, interrupt, settle
``seam``        two calls the inherited session makes, both of which never raise

Layering (docs/robot-architecture.md Sect. 7): this package sits beside ``robot/agent``
and above everything else. It may import ``robot/agent``, ``robot/events``,
``robot/state`` and ``robot/actions``. It may not import ``robot/safety``,
``robot/devices``, ``robot/vision`` or ``robot/simulator``.
"""

from robot.voice.expression import (
    FAILURE_EXPRESSION,
    MIN_INTERVAL_S,
    STATE_ANIMATIONS,
    STATE_EXPRESSIONS,
    ExpressionCoordinator,
)
from robot.voice.loop import ANSWER_REASON, RecordingSink, SpeechSink, VoiceLoop, VoiceSpeaker
from robot.voice.seam import (
    LOOP_ATTR,
    ROBOT_TOOL_NAMES,
    ConnectionSpeechSink,
    detach,
    handle_barge_in,
    handle_utterance,
    is_robot_session,
    voice_loop,
)
from robot.voice.state import (
    ALLOWED_TRANSITIONS,
    AudioState,
    VoiceStateMachine,
    can_transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ANSWER_REASON",
    "FAILURE_EXPRESSION",
    "LOOP_ATTR",
    "MIN_INTERVAL_S",
    "ROBOT_TOOL_NAMES",
    "STATE_ANIMATIONS",
    "STATE_EXPRESSIONS",
    "AudioState",
    "ConnectionSpeechSink",
    "ExpressionCoordinator",
    "RecordingSink",
    "SpeechSink",
    "VoiceLoop",
    "VoiceSpeaker",
    "VoiceStateMachine",
    "can_transition",
    "detach",
    "handle_barge_in",
    "handle_utterance",
    "is_robot_session",
    "voice_loop",
]
