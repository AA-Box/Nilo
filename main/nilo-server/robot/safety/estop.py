"""The emergency-stop latch.

Three rules, from docs/safety.md, and the code exists to make them literal:

1. **Stop is always accepted.** Engaging is never gated on queue state, capability
   negotiation, or whether anything is running.
2. **Stop is never queued.** This class holds no queue at all; the executor dispatches a
   stop ahead of everything pending.
3. **Stop is executed locally.** A backend-originated stop is a *request* to do sooner
   what the firmware watchdog would do anyway. Engaging the latch here does not stop a
   robot — it stops the backend from asking it to move, and tells the executor to try.

The latch is sticky. Once engaged, nothing new is admitted for that robot until something
calls :meth:`EmergencyStop.clear` explicitly; there is no timeout that lifts it, because a
stop that expires on its own is a stop nobody decided to end.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from robot.state.models import utcnow

logger = logging.getLogger(__name__)


class EmergencyStopState(BaseModel):
    """Why one robot is latched, and since when. Published with the event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot_id: str
    reason: str = "emergency stop"
    engaged_at: datetime = Field(default_factory=utcnow)
    #: Who engaged it, as free text. Not an :class:`ActionSource`: an operator pressing a
    #: button in a management UI is not one of the five action sources.
    engaged_by: str = "system"


class EmergencyStop:
    """Per-robot latches. Constructed per runtime; there is no process-wide stop state."""

    def __init__(self) -> None:
        self._engaged: dict[str, EmergencyStopState] = {}

    def engage(
        self,
        robot_id: str,
        reason: str = "emergency stop",
        *,
        engaged_by: str = "system",
        now: datetime | None = None,
    ) -> EmergencyStopState:
        """Latch a robot. Idempotent: re-engaging keeps the original reason and time.

        Keeping the first reason matters for an incident log — the interesting fact is
        what stopped the robot, not that a supervisor re-asserted the stop afterwards.
        """
        existing = self._engaged.get(robot_id)
        if existing is not None:
            return existing
        state = EmergencyStopState(
            robot_id=robot_id, reason=reason, engaged_by=engaged_by, engaged_at=now or utcnow()
        )
        self._engaged[robot_id] = state
        logger.warning("robot %s: EMERGENCY STOP engaged by %s (%s)", robot_id, engaged_by, reason)
        return state

    def clear(self, robot_id: str) -> bool:
        """Release the latch. Returns whether it was engaged."""
        released = self._engaged.pop(robot_id, None)
        if released is None:
            return False
        logger.warning("robot %s: emergency stop cleared (was: %s)", robot_id, released.reason)
        return True

    def engaged(self, robot_id: str) -> bool:
        return robot_id in self._engaged

    def state(self, robot_id: str) -> EmergencyStopState | None:
        return self._engaged.get(robot_id)

    def engaged_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._engaged))

    def __bool__(self) -> bool:
        return bool(self._engaged)

    def __repr__(self) -> str:
        return f"<EmergencyStop engaged={list(self.engaged_ids())}>"


__all__ = ["EmergencyStop", "EmergencyStopState"]
