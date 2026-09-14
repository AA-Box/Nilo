"""The endpoints that move a robot, and the rules that stand in front of them.

This is a change of position for this package. Through Phase 7 the admin API deliberately
had **no** endpoint that could actuate: an admin surface that could drive a robot would be
a second path to the hardware, and the authentication story was not finished. It is now
(:mod:`robot.api.security`), and a development dashboard that cannot move the robot it is
debugging is not a development dashboard.

What has *not* changed is where the decision is made. Every request here becomes a typed
:class:`~robot.actions.model.ActionSpec` submitted to the same executor, attributed to
:attr:`~robot.state.actions.ActionSource.USER`, and judged by the same safety policy as a
behaviour's own command. There is no privileged path: an operator with the admin token
cannot obtain what the policy would refuse a model.

    POST /api/robots/{id}/actions/move      {"distance_mm": 250}
    POST /api/robots/{id}/actions/turn      {"angle_deg": 90}
    POST /api/robots/{id}/actions/stop
    POST /api/robots/{id}/animations/{name}
    POST /api/robots/{id}/autonomy-mode     {"mode": "passive"}
    POST /api/robots/{id}/emergency-stop

Bodies are frozen pydantic models with ``extra="forbid"``, so a misspelled field is a 400
rather than a silently defaulted speed. Ranges are **not** duplicated here: the safety
policy owns the ceilings, and a request past one comes back as a 409 carrying the typed
rejection reason. One place decides what is allowed.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from robot.api.security import ApiError
from robot.state.actions import ActionRecord, ActionSource, ActionStatus

logger = logging.getLogger(__name__)


class ControlRequest(BaseModel):
    """Base for every control body: frozen, and an unknown field is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MoveRequest(ControlRequest):
    distance_mm: int
    speed_mmps: int = Field(default=200, gt=0)


class TurnRequest(ControlRequest):
    angle_deg: int
    speed_dps: int = Field(default=90, gt=0)


class StopRequest(ControlRequest):
    reason: str = Field(default="stopped from the management API", max_length=200)


class HeadRequest(ControlRequest):
    pitch_deg: int = 0
    yaw_deg: int = 0


class LiftRequest(ControlRequest):
    height_pct: int = Field(ge=0, le=100)


class LookAtRequest(ControlRequest):
    x_pct: int = Field(default=50, ge=0, le=100)
    y_pct: int = Field(default=50, ge=0, le=100)


class ExpressionRequest(ControlRequest):
    emotion: str = Field(max_length=40)
    intensity_pct: int = Field(default=100, ge=0, le=100)


class AnimationRequest(ControlRequest):
    duration_ms: int = Field(default=2000, gt=0, le=60_000)


class AutonomyRequest(ControlRequest):
    mode: str


class EmergencyStopRequest(ControlRequest):
    reason: str = Field(default="emergency stop from the management API", max_length=200)
    engaged_by: str = Field(default="operator", max_length=80)


def parse(model: type[ControlRequest], body: Any) -> Any:
    """Validate one request body. Raises a 400 :class:`ApiError` with a readable message."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ApiError(400, "the request body must be a JSON object")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem.get('loc', ())) or 'body'}: {problem.get('msg', 'invalid')}"
            for problem in exc.errors()
        )
        raise ApiError(400, problems or "invalid request body") from None


class RobotControl:
    """Control operations for one robot. Every one of them ends at the action executor."""

    def __init__(self, runtime: Any, robot_id: str) -> None:
        self.runtime = runtime
        self.robot_id = robot_id

    @property
    def handle(self) -> Any:
        """The semantic handle, attributed to a person rather than to a model.

        Attribution only — a different source does not buy a different answer from safety
        (docs/safety-model.md). It is what makes an incident log say an operator asked for this.
        """
        return self.runtime.actions.robot(self.robot_id).as_source(ActionSource.USER)

    # -- motion ---------------------------------------------------------------------------------

    async def move(self, request: MoveRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.move(request.distance_mm, request.speed_mmps, wait=False)
        return record

    async def turn(self, request: TurnRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.turn(request.angle_deg, request.speed_dps, wait=False)
        return record

    async def stop(self, request: StopRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.stop(request.reason, wait=False)
        return record

    async def head(self, request: HeadRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.head_angle(request.pitch_deg, request.yaw_deg, wait=False)
        return record

    async def look_at(self, request: LookAtRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.look_at(request.x_pct, request.y_pct, wait=False)
        return record

    async def lift(self, request: LiftRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.lift(request.height_pct, wait=False)
        return record

    # -- expression ------------------------------------------------------------------------------

    async def expression(self, request: ExpressionRequest) -> ActionRecord:
        record: ActionRecord = await self.handle.set_expression(request.emotion, request.intensity_pct, wait=False)
        return record

    async def animation(self, name: str, request: AnimationRequest) -> dict[str, Any]:
        """Play an animation through the engine when there is one, else as a single action.

        The engine is the better path: it knows the library, it arbitrates against whatever
        else owns the face, and it plays every step through the same semantic actions.
        """
        engine = self.runtime.animations(self.robot_id)
        if engine is not None and engine.library.get(name) is not None:
            playback = await engine.play(name)
            if playback is None:
                raise ApiError(409, f"{name} did not play: something else is using the face, or the robot is low on energy")
            return {"animation": name, "accepted": True, "via": "animation_engine"}
        record = await self.handle.play_animation(name, duration_ms=request.duration_ms, wait=False)
        return {"animation": name, "via": "action", **_accepted(record)}

    # -- mode and the latch ------------------------------------------------------------------------

    async def set_autonomy(self, request: AutonomyRequest) -> dict[str, Any]:
        from robot.behavior.base import AutonomyMode

        try:
            mode = AutonomyMode(request.mode.strip().lower())
        except ValueError:
            allowed = ", ".join(member.value for member in AutonomyMode)
            raise ApiError(400, f"unknown autonomy mode {request.mode!r}; choose one of {allowed}") from None
        await self.runtime.set_autonomy(mode)
        return {"mode": mode.value}

    async def emergency_stop(self, request: EmergencyStopRequest) -> dict[str, Any]:
        """Latch the robot: cancel everything, refuse everything, attempt a stop.

        A request, not a guarantee. The stop this sends may never arrive; the firmware
        watchdog is what actually stops a robot (docs/safety-model.md). Say so in the response
        rather than reporting success the backend cannot promise.
        """
        record = await self.runtime.actions.emergency_stop(
            self.robot_id, request.reason, engaged_by=request.engaged_by
        )
        return {
            "engaged": True,
            "reason": request.reason,
            "stop_requested": record is not None,
            "note": "the backend requested a stop; the firmware watchdog is the guarantee",
        }

    async def clear_emergency_stop(self) -> dict[str, Any]:
        cleared = await self.runtime.actions.clear_emergency_stop(self.robot_id)
        return {"engaged": not cleared, "cleared": cleared}

    async def cancel_all(self, reason: str = "cancelled from the management API") -> dict[str, Any]:
        records = await self.runtime.actions.cancel_all(self.robot_id, reason=reason)
        return {"cancelled": len(records), "action_ids": [record.action_id for record in records]}


def action_response(record: ActionRecord | None) -> tuple[dict[str, Any], int]:
    """One action record as a body and a status code.

    ``202`` for an accepted action — it has not finished, and a ``200`` would imply it had.
    ``409`` for a safety rejection, carrying the typed reason so a caller can branch on
    ``cliff_hazard`` rather than on a sentence.
    """
    if record is None:
        return {"error": "the robot did not accept the command"}, 502
    body = _accepted(record)
    if record.status is ActionStatus.REJECTED:
        reason = record.rejection.value if record.rejection else "refused"
        body["error"] = record.error.message if record.error else reason
        body["reason"] = reason
        return body, 409
    return body, 202


def _accepted(record: ActionRecord) -> dict[str, Any]:
    return {
        "action_id": record.action_id,
        "status": record.status.value,
        "type": record.action_type.value,
        "parameters": dict(record.parameters),
    }


__all__ = [
    "AnimationRequest",
    "AutonomyRequest",
    "ControlRequest",
    "EmergencyStopRequest",
    "ExpressionRequest",
    "HeadRequest",
    "LiftRequest",
    "LookAtRequest",
    "MoveRequest",
    "RobotControl",
    "StopRequest",
    "TurnRequest",
    "action_response",
    "parse",
]
