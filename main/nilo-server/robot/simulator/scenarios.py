"""Scenarios and failure injection: what happens to the robot, on a timeline.

A scenario is data — a start pose, a room, a fault set and a list of ``(at_s, do, args)``
steps — so a new one is a YAML file and not a Python change, and the same file replayed on
the deterministic clock produces the same run every time.

Failure injection lives here rather than in the device layer because a fault *is* a
scenario step: "the camera fails at t=8" and "a person walks in at t=8" are the same kind
of statement. CLI flags set the initial :class:`Faults`; a ``fault`` step changes it
mid-run.

Times are simulated seconds from the start of the run. Distances in step arguments are
integer millimetres and angles integer degrees, the same vocabulary the tools use.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from robot.simulator.state import RobotSimState
from robot.simulator.world import Cliff, Obstacle, Person, World, WorldObject, WorldSpec

logger = logging.getLogger(__name__)


class Faults(BaseModel):
    """Everything that can be made to go wrong, and is off by default."""

    model_config = ConfigDict(extra="forbid")

    #: Delay added to every frame the simulator sends, in milliseconds.
    packet_delay_ms: int = 0
    #: Tools that accept the call and never answer, so the server's timeout fires.
    tool_timeout: list[str] = Field(default_factory=list)
    #: Tools that answer with an MCP ``isError`` result.
    tool_error: list[str] = Field(default_factory=list)
    #: The drive motors fail: any motion in flight ends as ``motor_failure``.
    motor_failure: bool = False
    #: ``robot.camera.capture`` fails instead of returning a frame.
    camera_failure: bool = False
    #: Telemetry notifications are not sent. The session stays up and looks healthy.
    drop_notifications: bool = False
    #: Motion completions are not sent, while telemetry still is. The action the backend
    #: dispatched never reports back, so its watchdog is what has to end it.
    drop_motion_completion: bool = False
    #: Drop the WebSocket (without a close frame) at this simulated time.
    disconnect_at_s: float | None = None
    #: Whether a dropped connection is dialled again.
    reconnect: bool = True

    def timing_out(self, tool_name: str) -> bool:
        return tool_name in self.tool_timeout

    def erroring(self, tool_name: str) -> bool:
        return tool_name in self.tool_error


class Step(BaseModel):
    """One thing that happens, once, at ``at_s``."""

    model_config = ConfigDict(extra="forbid")

    at_s: float
    do: str
    args: dict[str, Any] = Field(default_factory=dict)


class Scenario(BaseModel):
    """A reproducible run: where the robot starts, what the room is, what happens."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    duration_s: float = 30.0
    # Off the dock by default: a robot parked on its charger reads as charging, which
    # is a surprising baseline for a scenario that is not about charging.
    start_x_mm: int = 600
    start_y_mm: int = 200
    start_yaw_deg: int = 0
    start_battery_pct: int = 85
    world: WorldSpec | None = None
    faults: Faults = Field(default_factory=Faults)
    steps: list[Step] = Field(default_factory=list)

    def apply_start(self, state: RobotSimState) -> None:
        state.x_m = self.start_x_mm / 1000.0
        state.y_m = self.start_y_mm / 1000.0
        state.yaw_rad = math.radians(self.start_yaw_deg)
        state.battery_pct = float(self.start_battery_pct)


class ScenarioHost(Protocol):
    """What a scenario is allowed to do to the running simulator."""

    world: World
    state: RobotSimState
    faults: Faults

    def command_move(self, distance_mm: int, speed_mmps: int) -> str: ...

    def command_turn(self, angle_deg: int, speed_dps: int) -> str: ...

    def command_follow(self, target_id: str, duration_ms: int, stop_distance_mm: int) -> str: ...

    def command_stop(self) -> None: ...

    async def drop_connection(self, *, reconnect: bool) -> None: ...

    async def say(self, text: str) -> None: ...

    async def interrupt(self) -> None: ...


class ScenarioRunner:
    """Plays a scenario's steps against a host as simulated time passes.

    :meth:`advance_to` is called from the simulator's tick loop with the current
    simulated time; every step whose ``at_s`` has passed runs, in order, exactly once.
    Driving it from the tick loop rather than from its own timer is what makes an
    accelerated or manual clock work: there is only one notion of time in the process.
    """

    def __init__(self, scenario: Scenario, host: ScenarioHost) -> None:
        self.scenario = scenario
        self.host = host
        self._pending = sorted(scenario.steps, key=lambda step: step.at_s)
        self._index = 0
        self._log: list[tuple[float, str]] = []

    @property
    def done(self) -> bool:
        return self._index >= len(self._pending)

    @property
    def history(self) -> tuple[tuple[float, str], ...]:
        return tuple(self._log)

    async def advance_to(self, now_s: float) -> None:
        while self._index < len(self._pending) and self._pending[self._index].at_s <= now_s:
            step = self._pending[self._index]
            self._index += 1
            await self._run(step, now_s)

    async def _run(self, step: Step, now_s: float) -> None:
        handler = _ACTIONS.get(step.do)
        if handler is None:
            logger.warning("scenario %s: unknown step %r, skipping", self.scenario.name, step.do)
            return
        try:
            await handler(self.host, step.args)
        except Exception as exc:  # a bad step must not kill the run
            logger.warning("scenario %s: step %r failed: %s", self.scenario.name, step.do, exc)
            return
        self._log.append((now_s, f"{step.do} {json.dumps(step.args, sort_keys=True)}"))
        logger.info("scenario %s t=%.1fs: %s %s", self.scenario.name, now_s, step.do, step.args)


# -- step implementations ----------------------------------------------------------------


async def _spawn_person(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.add_person(
        Person(
            id=str(args.get("id", "person-1")),
            x_m=int(args.get("x_mm", 1500)) / 1000.0,
            y_m=int(args.get("y_mm", 200)) / 1000.0,
            name=str(args.get("name", "person")),
        )
    )


async def _remove_person(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.remove_person(str(args.get("id", "person-1")))


async def _add_object(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.objects[str(args.get("id", "object-1"))] = WorldObject(
        id=str(args.get("id", "object-1")),
        x_m=int(args.get("x_mm", 1000)) / 1000.0,
        y_m=int(args.get("y_mm", 500)) / 1000.0,
        label=str(args.get("label", "object")),
    )


async def _remove_object(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.objects.pop(str(args.get("id", "object-1")), None)


async def _add_obstacle(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.add_obstacle(
        Obstacle(
            id=str(args.get("id", "obstacle-1")),
            x_m=int(args.get("x_mm", 1000)) / 1000.0,
            y_m=int(args.get("y_mm", 200)) / 1000.0,
            radius_m=int(args.get("radius_mm", 100)) / 1000.0,
        )
    )


async def _remove_obstacle(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.remove_obstacle(str(args.get("id", "obstacle-1")))


async def _add_cliff(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.add_cliff(
        Cliff(
            id=str(args.get("id", "cliff-1")),
            x0_m=int(args.get("x0_mm", 1000)) / 1000.0,
            y0_m=int(args.get("y0_mm", -500)) / 1000.0,
            x1_m=int(args.get("x1_mm", 1200)) / 1000.0,
            y1_m=int(args.get("y1_mm", 3500)) / 1000.0,
        )
    )


async def _remove_cliff(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.world.remove_cliff(str(args.get("id", "cliff-1")))


async def _set_battery(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.state.battery_pct = float(max(0, min(100, int(args.get("percent", 20)))))


async def _move(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.command_move(int(args.get("distance_mm", 500)), int(args.get("speed_mmps", 200)))


async def _turn(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.command_turn(int(args.get("angle_deg", 90)), int(args.get("speed_dps", 90)))


async def _follow(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.command_follow(
        str(args.get("target_id", "person-1")),
        int(args.get("duration_ms", 5000)),
        int(args.get("stop_distance_mm", 600)),
    )


async def _stop(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.command_stop()


async def _set_expression(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.state.emotion = str(args.get("emotion", "neutral"))
    host.state.emotion_intensity_pct = int(args.get("intensity_pct", 100))


async def _play_animation(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.state.animation = str(args.get("name", "idle_look_around"))
    host.state.animation_remaining_s = float(args.get("duration_s", 2.0))


async def _set_picked_up(host: ScenarioHost, args: dict[str, Any]) -> None:
    host.state.picked_up = bool(args.get("picked_up", True))


async def _fault(host: ScenarioHost, args: dict[str, Any]) -> None:
    for key, value in args.items():
        if not hasattr(host.faults, key):
            raise ValueError(f"unknown fault {key!r}")
        setattr(host.faults, key, value)


async def _disconnect(host: ScenarioHost, args: dict[str, Any]) -> None:
    await host.drop_connection(reconnect=bool(args.get("reconnect", True)))


async def _say(host: ScenarioHost, args: dict[str, Any]) -> None:
    """Somebody in the room says something to the robot.

    The recognizer's *output*, not audio: the simulator has no microphone and no model,
    and what a device with on-board recognition puts on the wire is this text.
    """
    await host.say(str(args.get("text", "hello")))


async def _interrupt(host: ScenarioHost, args: dict[str, Any]) -> None:
    """Somebody talks over the robot. The ``abort`` frame, as a scenario step."""
    await host.interrupt()


async def _log(host: ScenarioHost, args: dict[str, Any]) -> None:
    logger.info("scenario note: %s", args.get("message", ""))


_ACTIONS: dict[str, Any] = {
    "spawn_person": _spawn_person,
    "remove_person": _remove_person,
    "add_object": _add_object,
    "remove_object": _remove_object,
    "add_obstacle": _add_obstacle,
    "remove_obstacle": _remove_obstacle,
    "add_cliff": _add_cliff,
    "remove_cliff": _remove_cliff,
    "set_battery": _set_battery,
    "move": _move,
    "turn": _turn,
    "follow": _follow,
    "stop": _stop,
    "set_expression": _set_expression,
    "play_animation": _play_animation,
    "set_picked_up": _set_picked_up,
    "fault": _fault,
    "disconnect": _disconnect,
    "say": _say,
    "interrupt": _interrupt,
    "log": _log,
}

#: Every step name a scenario file may use.
STEP_ACTIONS: tuple[str, ...] = tuple(sorted(_ACTIONS))


# -- the built-in scenarios ---------------------------------------------------------------

BUILTIN_SCENARIOS: dict[str, Scenario] = {
    "idle": Scenario(
        name="idle",
        description="Nothing happens. Telemetry only — the baseline for a connection test.",
        duration_s=20.0,
    ),
    "person_enters_room": Scenario(
        name="person_enters_room",
        description="A person walks in front of the robot, which notices and reacts.",
        duration_s=24.0,
        steps=[
            Step(at_s=4.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 200}),
            Step(at_s=5.0, do="set_expression", args={"emotion": "happy", "intensity_pct": 80}),
            Step(at_s=6.0, do="play_animation", args={"name": "greet", "duration_s": 2.0}),
            Step(at_s=10.0, do="spawn_person", args={"id": "person-1", "x_mm": 900, "y_mm": 250}),
            Step(at_s=16.0, do="set_expression", args={"emotion": "curious", "intensity_pct": 60}),
        ],
    ),
    "conversation": Scenario(
        name="conversation",
        description=(
            "A person greets the robot, asks it to come closer, and talks over its answer. "
            "Needs a language model configured; with none, the robot stays quiet and "
            "everything else still runs."
        ),
        duration_s=30.0,
        steps=[
            Step(at_s=2.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 100}),
            Step(at_s=4.0, do="say", args={"text": "hello there"}),
            Step(at_s=10.0, do="say", args={"text": "come a little closer"}),
            Step(at_s=18.0, do="say", args={"text": "tell me what you can see"}),
            Step(at_s=19.0, do="interrupt", args={}),
            Step(at_s=21.0, do="say", args={"text": "never mind, stop"}),
        ],
    ),
    "person_leaves_room": Scenario(
        name="person_leaves_room",
        description="A person who was there walks out; the robot is alone again.",
        duration_s=20.0,
        steps=[
            Step(at_s=0.5, do="spawn_person", args={"id": "person-1", "x_mm": 1000, "y_mm": 200}),
            Step(at_s=1.0, do="set_expression", args={"emotion": "happy", "intensity_pct": 70}),
            Step(at_s=8.0, do="remove_person", args={"id": "person-1"}),
            Step(at_s=9.0, do="set_expression", args={"emotion": "sad", "intensity_pct": 40}),
            Step(at_s=12.0, do="play_animation", args={"name": "look_around", "duration_s": 3.0}),
        ],
    ),
    "robot_gets_bored": Scenario(
        name="robot_gets_bored",
        description="Nobody around for a while: idle animations, then a look around.",
        duration_s=40.0,
        steps=[
            Step(at_s=10.0, do="set_expression", args={"emotion": "bored", "intensity_pct": 30}),
            Step(at_s=12.0, do="play_animation", args={"name": "idle_fidget", "duration_s": 3.0}),
            Step(at_s=20.0, do="turn", args={"angle_deg": 90, "speed_dps": 60}),
            Step(at_s=26.0, do="turn", args={"angle_deg": -90, "speed_dps": 60}),
            Step(at_s=34.0, do="play_animation", args={"name": "sigh", "duration_s": 2.0}),
        ],
    ),
    "battery_low": Scenario(
        name="battery_low",
        description="Battery falls into the low band and keeps draining while the robot drives.",
        duration_s=30.0,
        start_battery_pct=40,
        steps=[
            Step(at_s=2.0, do="set_battery", args={"percent": 18}),
            Step(at_s=3.0, do="set_expression", args={"emotion": "tired", "intensity_pct": 50}),
            Step(at_s=4.0, do="move", args={"distance_mm": 1500, "speed_mmps": 150}),
            Step(at_s=20.0, do="set_battery", args={"percent": 5}),
        ],
    ),
    "obstacle_during_move": Scenario(
        name="obstacle_during_move",
        description="An obstacle appears in the path of a move already in flight.",
        duration_s=20.0,
        steps=[
            Step(at_s=2.0, do="move", args={"distance_mm": 2500, "speed_mmps": 200}),
            Step(at_s=5.0, do="add_obstacle", args={"id": "box", "x_mm": 1800, "y_mm": 200, "radius_mm": 120}),
            Step(at_s=12.0, do="remove_obstacle", args={"id": "box"}),
            Step(at_s=13.0, do="move", args={"distance_mm": 500, "speed_mmps": 200}),
        ],
    ),
    "cliff_during_move": Scenario(
        name="cliff_during_move",
        description="The floor ends mid-move; the cliff sensors assert and the motion fails.",
        duration_s=20.0,
        steps=[
            Step(at_s=2.0, do="move", args={"distance_mm": 2500, "speed_mmps": 200}),
            Step(
                at_s=5.0,
                do="add_cliff",
                args={"id": "stairs", "x0_mm": 1500, "y0_mm": -500, "x1_mm": 1700, "y1_mm": 3500},
            ),
            Step(at_s=10.0, do="set_expression", args={"emotion": "scared", "intensity_pct": 90}),
        ],
    ),
    "person_moves_across": Scenario(
        name="person_moves_across",
        description="A person walks left to right across the field of view, then back.",
        duration_s=30.0,
        steps=[
            Step(at_s=2.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": -700}),
            Step(at_s=5.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": -300}),
            Step(at_s=8.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 200}),
            Step(at_s=11.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 700}),
            Step(at_s=14.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 1100}),
            Step(at_s=18.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": 400}),
            Step(at_s=22.0, do="spawn_person", args={"id": "person-1", "x_mm": 1600, "y_mm": -400}),
        ],
    ),
    "person_approaches": Scenario(
        name="person_approaches",
        description="A person walks towards the robot and stops within touching distance.",
        duration_s=26.0,
        steps=[
            Step(at_s=2.0, do="spawn_person", args={"id": "person-1", "x_mm": 2600, "y_mm": 200}),
            Step(at_s=6.0, do="spawn_person", args={"id": "person-1", "x_mm": 2000, "y_mm": 200}),
            Step(at_s=10.0, do="spawn_person", args={"id": "person-1", "x_mm": 1500, "y_mm": 200}),
            Step(at_s=14.0, do="spawn_person", args={"id": "person-1", "x_mm": 1000, "y_mm": 200}),
            Step(at_s=18.0, do="spawn_person", args={"id": "person-1", "x_mm": 700, "y_mm": 200}),
            Step(at_s=20.0, do="set_expression", args={"emotion": "happy", "intensity_pct": 80}),
        ],
    ),
    "object_appears": Scenario(
        name="object_appears",
        description="Somebody puts a cube down in front of the robot, and takes it away again.",
        duration_s=26.0,
        steps=[
            Step(at_s=3.0, do="add_object", args={"id": "cube-1", "x_mm": 900, "y_mm": 150, "label": "cube"}),
            Step(at_s=5.0, do="set_expression", args={"emotion": "curious", "intensity_pct": 70}),
            Step(at_s=14.0, do="add_object", args={"id": "cube-1", "x_mm": 1200, "y_mm": 600, "label": "cube"}),
            Step(at_s=20.0, do="remove_object", args={"id": "cube-1"}),
            Step(at_s=21.0, do="set_expression", args={"emotion": "confused", "intensity_pct": 60}),
        ],
    ),
    "charger_found": Scenario(
        name="charger_found",
        description="Low battery, the robot drives onto the dock and starts charging.",
        duration_s=40.0,
        start_x_mm=1400,
        start_y_mm=200,
        start_yaw_deg=180,
        start_battery_pct=15,
        steps=[
            Step(at_s=2.0, do="set_expression", args={"emotion": "tired", "intensity_pct": 60}),
            Step(at_s=3.0, do="move", args={"distance_mm": 1200, "speed_mmps": 150}),
            Step(at_s=20.0, do="set_expression", args={"emotion": "content", "intensity_pct": 70}),
        ],
    ),
}


def get_scenario(name: str) -> Scenario:
    """A built-in scenario by name."""
    try:
        return BUILTIN_SCENARIOS[name].model_copy(deep=True)
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(sorted(BUILTIN_SCENARIOS))}") from None


def load_scenario_file(path: str | Path) -> Scenario:
    """A scenario from a YAML or JSON file with the same shape as the built-ins."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml

        data = yaml.safe_load(text)
    except ImportError:  # pragma: no cover - PyYAML is in requirements-dev
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a scenario file must contain a mapping")
    data.setdefault("name", Path(path).stem)
    return Scenario.model_validate(data)
