"""Injecting things that did not happen, so a developer can see what happens next.

    POST /api/robots/{id}/simulate/person      {"person_id": "ahmad", "x": 0.4}
    POST /api/robots/{id}/simulate/cliff       {"detected": true}
    POST /api/robots/{id}/simulate/low_battery {"percent": 8}

The dashboard's simulator panel. Every injection goes in through the **same doors real
perception uses** — a telemetry patch through :meth:`RobotRuntime.update_telemetry`, or a
vision event on the bus — so the world model, the behaviour engine, the safety policy and
the agent all see exactly what they would see from a device. There is no "simulated" flag
anywhere downstream, because a simulation that takes a different path through the code is
a simulation of different code.

**Injection is a control operation**, not a read. Telling the server there is no obstacle
in front of a robot that is about to drive is as dangerous as driving it, so these sit
behind the same token, the same loopback rule and the same rate limit as
``/actions/move`` (:mod:`robot.api.security`). A deployment that does not want them
disables control endpoints, and they go with it.

The sensor injections are **sticky**: a cliff stays reported until it is cleared, because
a flag that decays on its own is a flag a developer cannot reason about. Vision
injections are events and decay on the world model's own schedule, like real detections.

One consequence worth knowing before you use it: a connected device that reports the same
slice overwrites an injection on its next telemetry frame. The device is telling the truth
and the injection is not, so that is the right way round — but it means injection is for
what a device does *not* report: a sensor it has no hardware for, a robot that is not
connected, or a scenario a real one cannot be made to produce on a desk.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from robot.api.control import ControlRequest
from robot.events.types import ObjectDetected, PersonDetected
from robot.state.models import RobotBatteryState, RobotSensorState, RobotTelemetry
from robot.state.world import ImagePoint, obj, person

logger = logging.getLogger(__name__)

#: The distance reading an injected obstacle reports, in millimetres. Comfortably inside
#: the default ``min_obstacle_distance_mm`` so the refusal is the point of the injection.
OBSTACLE_DISTANCE_MM = 80.0

#: What the front sensor reads once an obstacle is cleared.
CLEAR_DISTANCE_MM = 1500.0


class PersonInjection(ControlRequest):
    person_id: str = Field(default="", max_length=120)
    track_id: str = Field(default="sim-person", max_length=120)
    display_name: str = Field(default="", max_length=120)
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)
    area: float = Field(default=0.08, ge=0.0, le=1.0)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class ObjectInjection(ControlRequest):
    track_id: str = Field(default="sim-object", max_length=120)
    label: str = Field(default="cube", max_length=80)
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)
    area: float = Field(default=0.04, ge=0.0, le=1.0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class FlagInjection(ControlRequest):
    """A sticky sensor flag: obstacle, cliff or touch. ``detected: false`` clears it."""

    detected: bool = True


class BatteryInjection(ControlRequest):
    percent: int = Field(default=8, ge=0, le=100)
    charging: bool = False


async def inject_person(runtime: Any, robot_id: str, request: PersonInjection) -> dict[str, Any]:
    """A person appears: the world entry *and* the event, in that order.

    Both halves, because that is what the vision pipeline does — it writes the entity and
    publishes the event, and a behaviour reads the first while a subscriber reads the
    second. An injection that published only the event would produce a robot that reacts
    to a person it cannot see.
    """
    await runtime.world.observe(
        robot_id,
        person(
            request.track_id,
            name=request.display_name or None,
            known=bool(request.person_id),
            confidence=request.confidence,
            image_point=_point(request.x, request.y, request.area),
            person_id=request.person_id or None,
        ),
    )
    await runtime.events.publish(
        PersonDetected(
            robot_id=robot_id,
            track_id=request.track_id,
            x=request.x,
            y=request.y,
            area=request.area,
            confidence=request.confidence,
            person_id=request.person_id or None,
            display_name=request.display_name,
        )
    )
    return {"injected": "person", "track_id": request.track_id, "person_id": request.person_id or None}


async def inject_object(runtime: Any, robot_id: str, request: ObjectInjection) -> dict[str, Any]:
    await runtime.world.observe(
        robot_id,
        obj(
            request.track_id,
            label=request.label,
            confidence=request.confidence,
            image_point=_point(request.x, request.y, request.area),
        ),
    )
    await runtime.events.publish(
        ObjectDetected(
            robot_id=robot_id,
            track_id=request.track_id,
            label=request.label,
            x=request.x,
            y=request.y,
            area=request.area,
            confidence=request.confidence,
        )
    )
    return {"injected": "object", "track_id": request.track_id, "label": request.label}


async def inject_obstacle(runtime: Any, robot_id: str, request: FlagInjection) -> dict[str, Any]:
    """Something close in front. Read by the safety policy, not by a behaviour."""
    distance = OBSTACLE_DISTANCE_MM if request.detected else CLEAR_DISTANCE_MM
    await _patch_sensors(runtime, robot_id, readings={"front_mm": distance})
    return {"injected": "obstacle", "detected": request.detected, "front_mm": distance}


async def inject_cliff(runtime: Any, robot_id: str, request: FlagInjection) -> dict[str, Any]:
    await _patch_sensors(runtime, robot_id, cliff_detected=request.detected)
    return {"injected": "cliff", "detected": request.detected}


async def inject_touch(runtime: Any, robot_id: str, request: FlagInjection) -> dict[str, Any]:
    await _patch_sensors(runtime, robot_id, touch_detected=request.detected)
    return {"injected": "touch", "detected": request.detected}


async def inject_battery(runtime: Any, robot_id: str, request: BatteryInjection) -> dict[str, Any]:
    await runtime.update_telemetry(
        robot_id,
        RobotTelemetry(battery=RobotBatteryState(percent=request.percent, charging=request.charging)),
    )
    return {"injected": "battery", "percent": request.percent, "charging": request.charging}


#: What the dashboard's simulator panel can inject, and the body each one takes.
INJECTIONS: dict[str, tuple[type[ControlRequest], Any]] = {
    "person": (PersonInjection, inject_person),
    "object": (ObjectInjection, inject_object),
    "obstacle": (FlagInjection, inject_obstacle),
    "cliff": (FlagInjection, inject_cliff),
    "touch": (FlagInjection, inject_touch),
    "low_battery": (BatteryInjection, inject_battery),
}


def _point(x: float, y: float, area: float) -> ImagePoint:
    """A normalized box of roughly ``area`` around ``x``/``y``, clamped to the frame."""
    side = min(0.9, max(0.01, area ** 0.5))
    return ImagePoint(
        x=min(1.0, max(0.0, x)),
        y=min(1.0, max(0.0, y)),
        width=side,
        height=side,
    )


async def _patch_sensors(runtime: Any, robot_id: str, **changes: Any) -> None:
    """Overlay a sensor change onto what the robot last reported, keeping the rest.

    A telemetry patch replaces a whole slice, so injecting a cliff by sending only
    ``cliff_detected`` would also erase the distance readings the safety policy reads —
    and the robot would be refused for stale data rather than for the cliff.
    """
    state = await runtime.get_state(robot_id)
    current = getattr(getattr(state, "telemetry", None), "sensors", None)
    base = current.model_dump() if current is not None else {}
    base.pop("updated_at", None)
    readings = dict(base.pop("readings", {}) or {})
    readings.update(changes.pop("readings", {}) or {})
    if not readings:
        readings = {"front_mm": CLEAR_DISTANCE_MM}
    await runtime.update_telemetry(
        robot_id, RobotTelemetry(sensors=RobotSensorState(**{**base, **changes}, readings=readings))
    )


__all__ = [
    "CLEAR_DISTANCE_MM",
    "INJECTIONS",
    "OBSTACLE_DISTANCE_MM",
    "BatteryInjection",
    "FlagInjection",
    "ObjectInjection",
    "PersonInjection",
    "inject_battery",
    "inject_cliff",
    "inject_object",
    "inject_obstacle",
    "inject_person",
    "inject_touch",
]
