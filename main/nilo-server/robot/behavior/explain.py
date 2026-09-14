"""Answering "why is the robot doing this?" without a robot.

Two things live here:

* :func:`explain_world` — score a world snapshot and return the decision. The same code
  path the engine takes at runtime, minus the execution, so the answer a CLI prints is the
  answer the robot would act on.
* The **situations**: a handful of named world snapshots (a person walks in, the battery
  is flat, nothing is happening) so the engine can be interrogated on a laptop with no
  server, no device and no camera.

The command-line front end is :mod:`robot.behavior.__main__`; keeping the logic here means
a management API can serve exactly the same structure without shelling out.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robot.behavior.base import AutonomyMode, Drives, NeutralDrives, RobotCommands
from robot.behavior.engine import BehaviorEngine, StaticWorld
from robot.behavior.scheduler import BehaviorDecision
from robot.behavior.tuning import BehaviorTuning
from robot.state.actions import ActionRecord, ActionPriority, ActionSource, ActionStatus, ActionType
from robot.state.models import RobotBatteryState, RobotSensorState, RobotTelemetry
from robot.state.world import (
    Entity,
    EntityType,
    Environment,
    ImagePoint,
    Interaction,
    Position,
    WorldState,
    person,
)

#: A fixed instant, so the built-in situations produce the same answer on every machine
#: and in every timezone. Freshness is measured against the snapshot's own clock.
FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

ROBOT_ID = "nilo-explain"


class NullRobot:
    """A :class:`~robot.behavior.base.RobotCommands` that records and commands nothing.

    Explaining is scoring, not acting. The CLI must never be able to move a real robot as
    a side effect of asking it a question, so the handle it is given cannot.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, command: str, **kwargs: Any) -> ActionRecord:
        # Named ``command`` rather than ``name`` because ``play_animation`` has a ``name``
        # argument of its own, and the collision is a TypeError at the worst moment.
        self.calls.append((command, kwargs))
        return ActionRecord(
            action_id=f"explain-{len(self.calls)}",
            robot_id=ROBOT_ID,
            action_type=ActionType.EXPRESSION,
            source=ActionSource.BEHAVIOR,
            priority=ActionPriority.NORMAL,
            status=ActionStatus.SUCCEEDED,
            parameters=dict(kwargs),
        )

    async def move(self, distance_mm: int, speed_mmps: int = 200, **kwargs: Any) -> ActionRecord:
        return self._record("move", distance_mm=distance_mm, speed_mmps=speed_mmps, **kwargs)

    async def turn(self, angle_deg: int, speed_dps: int = 90, **kwargs: Any) -> ActionRecord:
        return self._record("turn", angle_deg=angle_deg, speed_dps=speed_dps, **kwargs)

    async def stop(self, reason: str = "stop", **kwargs: Any) -> ActionRecord:
        return self._record("stop", reason=reason, **kwargs)

    async def look_at(self, x_pct: int = 50, y_pct: int = 50, **kwargs: Any) -> ActionRecord:
        return self._record("look_at", x_pct=x_pct, y_pct=y_pct, **kwargs)

    async def head_angle(self, pitch_deg: int = 0, yaw_deg: int = 0, **kwargs: Any) -> ActionRecord:
        return self._record("head_angle", pitch_deg=pitch_deg, yaw_deg=yaw_deg, **kwargs)

    async def set_expression(self, emotion: str, intensity_pct: int = 100, **kwargs: Any) -> ActionRecord:
        return self._record("set_expression", emotion=emotion, intensity_pct=intensity_pct, **kwargs)

    async def play_animation(self, name: str, **kwargs: Any) -> ActionRecord:
        return self._record("play_animation", name=name, **kwargs)

    async def follow(self, target_id: str, **kwargs: Any) -> ActionRecord:
        return self._record("follow", target_id=target_id, **kwargs)


def explain_world(
    world: WorldState,
    *,
    mode: AutonomyMode = AutonomyMode.NORMAL,
    tuning: BehaviorTuning | None = None,
    seed: int = 0,
    now: float = 0.0,
    drives: Drives | None = None,
    robot: RobotCommands | None = None,
) -> BehaviorDecision:
    """Score one snapshot and return the full decision, including the losers.

    ``now`` is the engine clock in seconds — it is what cooldowns and the idle timer are
    measured against, and passing it explicitly is what makes the answer reproducible.
    """
    engine = BehaviorEngine(
        world.robot_id or ROBOT_ID,
        robot or NullRobot(),
        StaticWorld(world),
        tuning=tuning,
        mode=mode,
        seed=seed,
        clock=lambda: now,
        drives=drives or NeutralDrives(),
    )
    return engine.evaluate()


# -- the built-in situations -------------------------------------------------------------------


def _telemetry(
    *,
    battery_percent: int = 80,
    charging: bool = False,
    touch: bool = False,
    now: datetime = FIXED_NOW,
) -> RobotTelemetry:
    return RobotTelemetry(
        battery=RobotBatteryState(percent=battery_percent, charging=charging, updated_at=now),
        sensors=RobotSensorState(touch_detected=touch, readings={"front_mm": 1500.0}, updated_at=now),
        updated_at=now,
    )


def _base(**telemetry: Any) -> WorldState:
    return WorldState(robot_id=ROBOT_ID, telemetry=_telemetry(**telemetry), updated_at=FIXED_NOW)


def situation_quiet_room() -> WorldState:
    """Nothing at all. The baseline: what does a robot do when there is nothing to do?"""
    return _base()


def situation_person_arrives() -> WorldState:
    """A familiar person has just been detected, slightly left of centre."""
    visitor = person(
        "person-1",
        name="Ahmad",
        known=True,
        position=Position(distance_mm=1400, bearing_deg=12.0),
        image_point=ImagePoint(x=0.35, y=0.45, width=0.2, height=0.5),
        now=FIXED_NOW,
    )
    return _base().observe(visitor, now=FIXED_NOW).attend_to("person-1", now=FIXED_NOW)


def situation_low_battery() -> WorldState:
    """Eight percent, with a dock the robot knows about. Docking should win outright."""
    dock = Entity(
        id="dock-1",
        type=EntityType.LOCATION,
        attributes={"label": "charger"},
        position=Position(distance_mm=2200, bearing_deg=-30.0),
        first_seen=FIXED_NOW,
        last_seen=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    visitor = person("person-1", name="Ahmad", known=True, now=FIXED_NOW)
    world = _base(battery_percent=8).observe(dock, now=FIXED_NOW)
    return world.observe(visitor, now=FIXED_NOW)


def situation_charging() -> WorldState:
    """On the dock, half charged."""
    return _base(battery_percent=52, charging=True)


def situation_touched() -> WorldState:
    """Somebody is stroking the robot's back."""
    return _base(touch=True)


def situation_bored() -> WorldState:
    """An interaction that ended a long time ago, and nothing since."""
    world = _base()
    finished = Interaction(
        id="chat-1",
        person_id="person-1",
        started_at=FIXED_NOW.replace(hour=11, minute=0),
        ended_at=FIXED_NOW.replace(hour=11, minute=5),
        turns=6,
    )
    return world.start_interaction(finished, now=FIXED_NOW).end_interaction(now=FIXED_NOW)


def situation_loud_noise() -> WorldState:
    """A bang from the right."""
    return _base().with_environment(
        Environment(sound_level=0.8, sound_direction_deg=-40.0, label="bang", updated_at=FIXED_NOW),
        now=FIXED_NOW,
    )


def situation_new_object() -> WorldState:
    """An unfamiliar thing on the floor."""
    from robot.state.world import obj

    cube = obj(
        "object-1",
        label="cube",
        position=Position(distance_mm=700, bearing_deg=-8.0),
        image_point=ImagePoint(x=0.6, y=0.7, width=0.1, height=0.1),
        now=FIXED_NOW,
    )
    return _base().observe(cube, now=FIXED_NOW)


#: Named world snapshots the CLI can explain. Adding one is adding a function here.
SITUATIONS: dict[str, Callable[[], WorldState]] = {
    "quiet_room": situation_quiet_room,
    "person_arrives": situation_person_arrives,
    "low_battery": situation_low_battery,
    "charging": situation_charging,
    "touched": situation_touched,
    "bored": situation_bored,
    "loud_noise": situation_loud_noise,
    "new_object": situation_new_object,
}


def get_situation(name: str) -> WorldState:
    try:
        return SITUATIONS[name]()
    except KeyError:
        raise KeyError(f"unknown situation {name!r}; known: {', '.join(sorted(SITUATIONS))}") from None


def load_world(path: str | Path) -> WorldState:
    """A world snapshot from a JSON file — the shape :meth:`WorldState.model_dump_json` writes."""
    return WorldState.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = [
    "FIXED_NOW",
    "SITUATIONS",
    "NullRobot",
    "explain_world",
    "get_situation",
    "load_world",
]
