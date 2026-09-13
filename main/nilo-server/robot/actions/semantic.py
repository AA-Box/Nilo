"""The semantic surface: ``await robot.move(...)``, and nothing below it.

This is the vocabulary every caller above the action layer speaks — behaviours, the
management API, and (through the Phase 4 bridge) the LLM. It is deliberately thin: each
method builds a typed spec, hands it to :class:`~robot.actions.executor.RobotActionExecutor`
and returns. There is no second path to a device, and no method here takes a motor, a
servo, a PWM duty cycle or a wheel speed, because the vocabulary *is* the safety boundary
(docs/robot-architecture.md Sect. 3).

    handle = executor.robot("nilo-sim-01").as_source(ActionSource.LLM)
    record = await handle.move(distance_mm=300)      # waits for the robot to finish
    record = await handle.turn(angle_deg=90, wait=False)  # returns as soon as it is admitted

Every method returns an :class:`~robot.state.actions.ActionRecord`, including when safety
refused: ``record.status is ActionStatus.REJECTED`` and ``record.rejection`` is the typed
reason. Nothing raises for a rejection — a refusal is an outcome, not an error, and a
caller that treats it as an exception ends up swallowing it.

``wait=True`` is the right default for a script or a behaviour. It is the **wrong** default
for an LLM tool handler, which must return in microseconds: the chat loop awaits tool
futures sequentially on a five-worker pool with no cancellation, so a handler that waits
for motion pins a worker for the whole ``tool_call_timeout`` (``core/connection.py``). The
bridge passes ``wait=False``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from robot.actions.model import (
    DISPATCH_TIMEOUT_S,
    ActionSpec,
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    HeadAngleAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    RobotAction,
    StopAction,
    TurnAction,
)
from robot.state.actions import ActionPriority, ActionRecord, ActionSource

if TYPE_CHECKING:  # pragma: no cover - avoids a cycle that only the type checker sees
    from robot.actions.executor import RobotActionExecutor

logger = logging.getLogger(__name__)

#: Slack added to an action's own timeout before :meth:`RobotHandle` gives up waiting.
#: The watchdog should always settle the action first; this is the net under the net.
WAIT_MARGIN_S = 2.0


class RobotHandle:
    """One robot, addressed semantically. Cheap to construct; holds no state of its own."""

    __slots__ = ("_executor", "priority", "robot_id", "source")

    def __init__(
        self,
        executor: RobotActionExecutor,
        robot_id: str,
        *,
        source: ActionSource = ActionSource.USER,
        priority: ActionPriority = ActionPriority.NORMAL,
    ) -> None:
        self._executor = executor
        self.robot_id = robot_id
        self.source = source
        self.priority = priority

    def as_source(self, source: ActionSource, priority: ActionPriority | None = None) -> RobotHandle:
        """A handle onto the same robot that attributes its actions to another source.

        Attribution only. A different source does not buy a different answer from safety
        (docs/safety.md): the LLM cannot obtain what a behaviour would be refused.
        """
        return RobotHandle(
            self._executor, self.robot_id, source=source, priority=priority or self.priority
        )

    # -- motion ---------------------------------------------------------------------------

    async def move(
        self,
        distance_mm: int,
        speed_mmps: int = 200,
        *,
        wait: bool = True,
        priority: ActionPriority | None = None,
        timeout_s: float | None = None,
    ) -> ActionRecord:
        """Drive straight. Negative distances drive backwards."""
        return await self._run(
            MoveAction(distance_mm=distance_mm, speed_mmps=speed_mmps),
            wait=wait,
            priority=priority,
            timeout_s=timeout_s,
        )

    async def turn(
        self,
        angle_deg: int,
        speed_dps: int = 90,
        *,
        wait: bool = True,
        priority: ActionPriority | None = None,
        timeout_s: float | None = None,
    ) -> ActionRecord:
        """Turn in place. Positive angles turn left."""
        return await self._run(
            TurnAction(angle_deg=angle_deg, speed_dps=speed_dps),
            wait=wait,
            priority=priority,
            timeout_s=timeout_s,
        )

    async def stop(self, reason: str = "stop requested", *, wait: bool = True) -> ActionRecord:
        """Cancel the motion in flight. Never queued, never refused for a hazard."""
        return await self._run(
            StopAction(reason=reason), wait=wait, priority=ActionPriority.EMERGENCY
        )

    async def follow(
        self,
        target_id: str,
        *,
        duration_ms: int = 5000,
        stop_distance_mm: int = 600,
        wait: bool = True,
        priority: ActionPriority | None = None,
    ) -> ActionRecord:
        """Keep a tracked target framed, for a bounded time."""
        return await self._run(
            FollowTargetAction(
                target_id=target_id, duration_ms=duration_ms, stop_distance_mm=stop_distance_mm
            ),
            wait=wait,
            priority=priority,
        )

    # -- head, lift --------------------------------------------------------------------------

    async def look_at(
        self, x_pct: int = 50, y_pct: int = 50, *, wait: bool = True, priority: ActionPriority | None = None
    ) -> ActionRecord:
        """Point the head at a spot in the camera frame, in percent of width and height."""
        return await self._run(LookAtAction(x_pct=x_pct, y_pct=y_pct), wait=wait, priority=priority)

    async def head_angle(
        self, pitch_deg: int = 0, yaw_deg: int = 0, *, wait: bool = True, priority: ActionPriority | None = None
    ) -> ActionRecord:
        """Point the head at an absolute pitch and yaw."""
        return await self._run(
            HeadAngleAction(pitch_deg=pitch_deg, yaw_deg=yaw_deg), wait=wait, priority=priority
        )

    async def lift(
        self, height_pct: int, *, wait: bool = True, priority: ActionPriority | None = None
    ) -> ActionRecord:
        """Raise or lower the lift."""
        return await self._run(LiftAction(height_pct=height_pct), wait=wait, priority=priority)

    # -- face, camera --------------------------------------------------------------------------

    async def set_expression(
        self,
        emotion: str,
        intensity_pct: int = 100,
        *,
        wait: bool = True,
        priority: ActionPriority | None = None,
    ) -> ActionRecord:
        """Show an emotion on the face."""
        return await self._run(
            ExpressionAction(emotion=emotion, intensity_pct=intensity_pct), wait=wait, priority=priority
        )

    async def play_animation(
        self,
        name: str,
        *,
        duration_ms: int = 2000,
        wait: bool = True,
        priority: ActionPriority | None = None,
    ) -> ActionRecord:
        """Play a named, pre-authored animation."""
        return await self._run(
            AnimationAction(name=name, duration_ms=duration_ms), wait=wait, priority=priority
        )

    async def capture_image(
        self, question: str | None = None, *, wait: bool = True, priority: ActionPriority | None = None
    ) -> ActionRecord:
        """Capture one camera frame, optionally asking the vision endpoint about it."""
        return await self._run(CaptureImageAction(question=question), wait=wait, priority=priority)

    # -- introspection ----------------------------------------------------------------------------

    async def emergency_stop(self, reason: str = "emergency stop", *, engaged_by: str = "system") -> ActionRecord | None:
        """Latch this robot, cancel everything, attempt a stop."""
        return await self._executor.emergency_stop(self.robot_id, reason, engaged_by=engaged_by)

    async def clear_emergency_stop(self) -> bool:
        return await self._executor.clear_emergency_stop(self.robot_id)

    @property
    def emergency_stopped(self) -> bool:
        return self._executor.emergency_stopped(self.robot_id)

    def pending(self) -> tuple[ActionRecord, ...]:
        return self._executor.pending(self.robot_id)

    def running(self) -> tuple[ActionRecord, ...]:
        return self._executor.running(self.robot_id)

    async def cancel_all(self, reason: str = "cancelled by request") -> tuple[ActionRecord, ...]:
        return await self._executor.cancel_all(self.robot_id, reason=reason)

    # -- the one place that submits ------------------------------------------------------------------

    async def _run(
        self,
        spec: ActionSpec,
        *,
        wait: bool,
        priority: ActionPriority | None = None,
        timeout_s: float | None = None,
    ) -> ActionRecord:
        action: RobotAction = await self._executor.submit(
            spec,
            self.robot_id,
            source=self.source,
            priority=priority or self.priority,
            timeout_s=timeout_s,
        )
        if not wait or action.is_terminal:
            return action.record()
        budget = action.timeout_s + DISPATCH_TIMEOUT_S + WAIT_MARGIN_S
        try:
            return await action.wait(timeout=budget)
        except (TimeoutError, asyncio.TimeoutError):
            # The watchdog should have settled this. Reaching here means the supervisor
            # itself did not run — worth a loud line, not a silent hang.
            logger.error(
                "robot %s: %s was still unsettled %.1fs after dispatch; the watchdog did not fire",
                self.robot_id,
                spec.describe(),
                budget,
            )
            return action.record()

    def __repr__(self) -> str:
        return f"<RobotHandle {self.robot_id} source={self.source.value}>"


__all__ = ["WAIT_MARGIN_S", "RobotHandle"]
