"""What the model is allowed to ask for, and when.

Four classes, because "can the LLM do this?" has four different answers and collapsing
them into one boolean is how a chat model ends up driving a robot across a room because
somebody in the next room said something that sounded like a request:

``READ_ONLY``    it reads state. Battery, pose, a memory lookup. Always allowed.
``EXPRESSIVE``  it changes the face or plays an animation. Nothing moves the base.
``MOTION``      it drives, turns or points the head. Subject to autonomy and to safety.
``PRIVILEGED``  it captures an image, or writes something into long-term memory about a
                person. Allowed only on a turn a person actually started.

The classification is a property of the tool; the *policy* — which classes are allowed on
which kind of turn — is configuration, and :class:`ToolPolicy` is where a deployment
changes it without touching a tool.

Two rules are not configurable, because a configuration that can remove them is a
configuration that will:

* **Stop is always allowed.** A robot that will not stop because its autonomy mode is
  ``OFF`` is the wrong failure, the same reasoning as
  :class:`~robot.actions.model.StopAction` never being refused for a hazard
  (docs/safety.md).
* **A permit is not a safety decision.** Everything a tool is permitted to do still goes
  through the action layer and its safety policy. This module can only take capability
  away; it can never grant any (docs/robot-architecture.md Sect. 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from robot.behavior.base import AutonomyMode


class ToolPermission(str, Enum):
    """How dangerous a tool is. Ordered by how much of the world it can change."""

    READ_ONLY = "read_only"
    EXPRESSIVE = "expressive"
    MOTION = "motion"
    PRIVILEGED = "privileged"


class TurnOrigin(str, Enum):
    """Who started the turn the model is answering.

    The distinction the policy turns on: a person asking the robot to come closer is not
    the same event as a behaviour deciding the robot should say hello, even when both end
    up calling the same model with the same tools.
    """

    #: A person spoke to the robot. Direct authorization for this turn.
    USER = "user"
    #: The behaviour engine asked for speech (:class:`~robot.agent.speech.SpeakIntent`).
    BEHAVIOR = "behavior"
    #: An operator or a scheduled task. Authorized, but not by a person in the room.
    SYSTEM = "system"

    @property
    def is_authorized_by_a_person(self) -> bool:
        return self is TurnOrigin.USER


#: Tools the policy may never refuse, whatever the mode or the origin. One entry, and it
#: is the one that stops the robot.
ALWAYS_ALLOWED: frozenset[str] = frozenset({"robot_stop"})


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Whether a tool call may proceed, and why not when it may not."""

    allowed: bool
    reason: str = ""

    @classmethod
    def permit(cls) -> PolicyDecision:
        return cls(True)

    @classmethod
    def refuse(cls, reason: str) -> PolicyDecision:
        return cls(False, reason)


@dataclass(frozen=True)
class ToolPolicy:
    """Which permission classes are usable on which kind of turn.

    The defaults are the example from the design note: read-only and expressive tools are
    autonomous, motion is subject to the autonomy mode, and privileged tools need a person
    to have started the turn.

        ToolPolicy()                                    # the defaults
        ToolPolicy(autonomous=frozenset())              # no autonomous tool use at all
        ToolPolicy(motion_min_autonomy=AutonomyMode.FULL)
    """

    #: Usable when nobody started the turn — a behaviour-triggered or system turn.
    autonomous: frozenset[ToolPermission] = field(
        default=frozenset({ToolPermission.READ_ONLY, ToolPermission.EXPRESSIVE})
    )
    #: Usable when a person started the turn by speaking.
    user_authorized: frozenset[ToolPermission] = field(
        default=frozenset(
            {
                ToolPermission.READ_ONLY,
                ToolPermission.EXPRESSIVE,
                ToolPermission.MOTION,
                ToolPermission.PRIVILEGED,
            }
        )
    )
    #: The lowest autonomy mode in which a motion tool may be called at all, whoever
    #: asked. A robot in ``OFF`` or ``PASSIVE`` does not drive because a model asked it to,
    #: and the model is told so rather than silently ignored. A deployment that wants a
    #: person to be able to drive the robot in any mode sets this to ``AutonomyMode.OFF``.
    motion_min_autonomy: AutonomyMode = AutonomyMode.NORMAL
    #: Tool names that bypass every check above.
    always_allowed: frozenset[str] = ALWAYS_ALLOWED

    def permitted(self, origin: TurnOrigin) -> frozenset[ToolPermission]:
        """The permission classes usable on a turn with this origin."""
        return self.user_authorized if origin.is_authorized_by_a_person else self.autonomous

    def decide(
        self,
        tool_name: str,
        permission: ToolPermission,
        *,
        origin: TurnOrigin,
        mode: AutonomyMode,
    ) -> PolicyDecision:
        """Whether ``tool_name`` may be called on this turn. Never raises."""
        if tool_name in self.always_allowed:
            return PolicyDecision.permit()
        allowed = self.permitted(origin)
        if permission not in allowed:
            return PolicyDecision.refuse(
                f"{permission.value} tools are not available on a {origin.value} turn"
            )
        if not origin.is_authorized_by_a_person and mode is AutonomyMode.OFF:
            # "Nothing autonomous. Direct commands only." A behaviour-triggered turn is
            # not a direct command, so it gets nothing but the tool that stops the robot.
            return PolicyDecision.refuse("autonomy is off; the robot does nothing on its own")
        if permission is ToolPermission.MOTION and mode.rank < self.motion_min_autonomy.rank:
            return PolicyDecision.refuse(
                f"motion needs autonomy {self.motion_min_autonomy.value} or higher; "
                f"the robot is in {mode.value}"
            )
        return PolicyDecision.permit()


__all__ = [
    "ALWAYS_ALLOWED",
    "PolicyDecision",
    "ToolPermission",
    "ToolPolicy",
    "TurnOrigin",
]
