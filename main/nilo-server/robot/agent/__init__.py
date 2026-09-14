"""The robot agent: a language model with a body, and a short list of things it may do.

    from robot.agent import RobotAgent, SpeakIntent, ToolPolicy

    agent = RobotAgent("nilo-sim-01", runtime, llm=provider, memory=memory)
    turn = await agent.respond("come closer", person_id="ahmad")

Five modules, and the split is the design:

``permissions``  four permission classes and the configurable policy over them
``tools``        the fourteen semantic tools, their schemas, and the one place they run
``context``      what the model is told before it answers — and what it is not told
``speech``       :class:`~robot.agent.speech.SpeakIntent` and the single arbitration path
``agent``        conversation, planning, tool selection, interruption and the fallback
``bridge``       registration into the inherited flat tool namespace, plus collision detection

The model is responsible for conversation, interpretation, planning, tool selection and
wording. It is responsible for nothing else: PID loops, raw motors, safety, behaviour
scheduling, vision tracking and every timing-critical operation live below it, in code that
keeps working when the model is unavailable — and a test proves the robot still answers
when it is (docs/robot-agent.md).

This package **wraps** the inherited LLM provider architecture; it does not add a second
one. Anything with ``response_with_functions`` is a provider here, which is every module
under ``core/providers/llm/`` and a twenty-line fake in a test.

Layering (docs/robot-architecture.md Sect. 7): the agent sits at the top. It may import
``robot/state``, ``robot/events``, ``robot/actions``, ``robot/animation``, ``robot/memory``
and ``robot/behavior``. It may **not** import ``robot/safety`` — no layer above the policy
may reach the policy — nor ``robot/devices``, ``robot/vision`` or ``robot/simulator``.
"""

from robot.agent.agent import (
    DEFAULT_LLM_TIMEOUT_S,
    FALLBACK_REPLIES,
    MAX_HISTORY,
    MAX_TOOL_ROUNDS,
    AgentSpeaker,
    AgentTurn,
    Conversation,
    LLMProvider,
    LLMUnavailable,
    RobotAgent,
)
from robot.agent.context import PersonContext, Restrictions, RobotContext, build_context
from robot.agent.permissions import (
    ALWAYS_ALLOWED,
    PolicyDecision,
    ToolPermission,
    ToolPolicy,
    TurnOrigin,
)
from robot.agent.speech import (
    DEFAULT_REASON_COOLDOWN_S,
    RecordingSpeaker,
    SpeakIntent,
    Speaker,
    SpeechArbiter,
    SpeechDecision,
    SpeechPriority,
    SpeechRequest,
)
from robot.agent.tools import (
    DEFAULT_TOOL_TIMEOUT_S,
    TOOL_NAMES,
    TOOL_SPECS,
    TOOLS_BY_NAME,
    RobotToolkit,
    RobotToolSpec,
    ToolError,
    ToolOutcome,
)

__all__ = [
    "ALWAYS_ALLOWED",
    "DEFAULT_LLM_TIMEOUT_S",
    "DEFAULT_REASON_COOLDOWN_S",
    "DEFAULT_TOOL_TIMEOUT_S",
    "FALLBACK_REPLIES",
    "MAX_HISTORY",
    "MAX_TOOL_ROUNDS",
    "TOOLS_BY_NAME",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "AgentSpeaker",
    "AgentTurn",
    "Conversation",
    "LLMProvider",
    "LLMUnavailable",
    "PersonContext",
    "PolicyDecision",
    "RecordingSpeaker",
    "Restrictions",
    "RobotAgent",
    "RobotContext",
    "RobotToolSpec",
    "RobotToolkit",
    "SpeakIntent",
    "Speaker",
    "SpeechArbiter",
    "SpeechDecision",
    "SpeechPriority",
    "SpeechRequest",
    "ToolError",
    "ToolOutcome",
    "ToolPermission",
    "ToolPolicy",
    "TurnOrigin",
]
