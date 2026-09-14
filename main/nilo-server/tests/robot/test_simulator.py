"""The simulator without a socket: clock, world, physics, camera, tools and scenarios.

Every test here runs on a :class:`~robot.simulator.clock.ManualClock`, so "after three
seconds of driving" is an exact statement and not a race. The socket half is covered by
``tests/integration/test_simulator_e2e.py``; nothing in this file connects to anything,
which is why it runs on the dev-only dependency slice.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any

import pytest

from robot.simulator.camera import DEFAULT_HEIGHT, DEFAULT_WIDTH, CameraFailure, FixtureCamera, render_frame
from robot.simulator.clock import ManualClock, RealClock, build_clock
from robot.simulator.device import SimulatedRobot, SimulatorConfig
from robot.simulator.scenarios import (
    BUILTIN_SCENARIOS,
    STEP_ACTIONS,
    Scenario,
    ScenarioRunner,
    Step,
    get_scenario,
    load_scenario_file,
)
from robot.simulator.state import MotionOutcome, RobotProfile, RobotSimState
from robot.simulator.tools import TOOL_NAMES, TOOL_SPECS, TOOLS_BY_NAME
from robot.simulator.world import Cliff, Obstacle, Person, World, WorldObject, default_room

#: Parameter names no robot tool may use: they would hand raw actuator control to a caller.
ACTUATOR_DENYLIST = ("pwm", "duty", "servo", "wheel", "voltage", "torque", "current", "step_us")


def offline(scenario: Scenario | None = None, **config: Any) -> SimulatedRobot:
    """A simulator that never connects: no status port, a clock a test advances by hand."""
    defaults: dict[str, Any] = {"status_port": 0, "telemetry_ms": 1000, "tick_ms": 100}
    settings = SimulatorConfig(**{**defaults, **config})
    return SimulatedRobot(settings, scenario=scenario, clock=ManualClock())


async def drive(robot: SimulatedRobot, seconds: float, *, dt: float = 0.1) -> None:
    """Advance a manual-clock simulator by ``seconds`` of simulated time, one tick at a time."""
    clock = robot.clock
    assert isinstance(clock, ManualClock)
    for _ in range(int(round(seconds / dt))):
        await clock.advance(dt)
        await robot.step(dt)


# -- the clock ------------------------------------------------------------------------------


async def test_manual_clock_only_moves_when_advanced() -> None:
    clock = ManualClock()
    assert clock.now() == 0.0
    ticks: list[float] = []

    async def ticker() -> None:
        for _ in range(3):
            await clock.sleep(1.0)
            ticks.append(clock.now())

    import asyncio

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    assert ticks == []
    await clock.advance(2.5)
    assert ticks == [1.0, 2.0]
    await clock.advance(1.0)
    assert ticks == [1.0, 2.0, 3.0]
    await task
    assert clock.now() == pytest.approx(3.5)


async def test_manual_clock_refuses_to_run_backwards() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError):
        await clock.advance(-1.0)


async def test_real_clock_speed_scales_simulated_time() -> None:
    fast = RealClock(speed=50.0)
    await fast.sleep(0.5)  # 10 ms of real time
    assert fast.now() > 0.0
    assert fast.speed == 50.0
    with pytest.raises(ValueError):
        RealClock(speed=0)


def test_build_clock_picks_the_requested_kind() -> None:
    assert isinstance(build_clock(2.0), RealClock)
    assert isinstance(build_clock(manual=True), ManualClock)


# -- the scenario timeline -------------------------------------------------------------------
#
# The timeline is the one thing in the simulator that used to depend on how fast the host
# was: a scripted step at t=2 s on an eight-times clock lands a quarter of a second into
# the run, which on a loaded CI machine is still inside the WebSocket handshake. The step
# fired into a socket nobody was listening on, its effects were never reported, and the
# test waiting for them waited until it gave up. These tests pin the fix.


async def test_a_scenario_waits_for_the_server_to_discover_the_robot() -> None:
    robot = offline(
        Scenario(
            name="scripted",
            duration_s=0.0,
            steps=[Step(at_s=1.0, do="spawn_person", args={"id": "person-1"})],
        )
    )
    robot._expects_server = True  # what run() sets: there is a server to wait for

    await drive(robot, 3.0)
    assert robot.world.people == {}, "a step fired before the server knew the robot existed"
    assert robot.status()["scenario"]["started"] is False

    robot._discovered.set()
    await drive(robot, 0.2)  # the timeline starts here, at t=3.2s
    assert robot.world.people == {}, "the timeline restarted from zero, not from discovery"

    await drive(robot, 1.0)
    assert "person-1" in robot.world.people
    assert robot.status()["scenario"]["started"] is True


async def test_a_scenario_offline_runs_from_zero_exactly_as_before() -> None:
    """An offline simulator — a test driving step() by hand — has no server to wait for."""
    robot = offline(
        Scenario(
            name="scripted",
            duration_s=0.0,
            steps=[Step(at_s=1.0, do="spawn_person", args={"id": "person-1"})],
        )
    )
    await drive(robot, 0.5)
    assert robot.world.people == {}
    await drive(robot, 0.7)
    assert "person-1" in robot.world.people


async def test_the_timeline_does_not_restart_when_the_robot_reconnects() -> None:
    """A scenario is a story, not a loop: a dropped session does not replay it."""
    robot = offline(
        Scenario(
            name="scripted",
            duration_s=0.0,
            steps=[Step(at_s=1.0, do="spawn_person", args={"id": "person-1"})],
        )
    )
    robot._expects_server = True
    robot._discovered.set()
    await drive(robot, 1.5)
    assert "person-1" in robot.world.people

    robot.world.remove_person("person-1")
    robot._discovered.clear()   # the session dropped
    robot._discovered.set()     # and came back
    await drive(robot, 2.0)
    assert robot.world.people == {}, "the scenario replayed after a reconnect"


# -- the world -------------------------------------------------------------------------------


def test_distance_is_capped_at_the_sensor_range() -> None:
    world = World()
    assert world.distance(0.6, 1.5, 0.0) == pytest.approx(world.max_range_m)


def test_an_obstacle_shortens_the_reading() -> None:
    world = World()
    world.add_obstacle(Obstacle(id="box", x_m=1.6, y_m=1.5, radius_m=0.1))
    assert world.distance(0.6, 1.5, 0.0) == pytest.approx(0.9, abs=0.01)
    assert world.remove_obstacle("box") is True
    assert world.distance(0.6, 1.5, 0.0) == pytest.approx(world.max_range_m)


def test_walls_are_seen_and_block_a_path() -> None:
    world = World(default_room(1.0, 1.0))
    assert world.distance(0.5, 0.5, 0.0) == pytest.approx(0.5, abs=0.01)
    assert world.crosses_wall(0.5, 0.5, 1.5, 0.5) is True
    assert world.crosses_wall(0.2, 0.5, 0.8, 0.5) is False


def test_cliffs_are_regions_not_points() -> None:
    world = World()
    world.add_cliff(Cliff(id="edge", x0_m=2.0, y0_m=0.0, x1_m=2.2, y1_m=3.0))
    assert world.cliff_at(2.1, 1.0) == "edge"
    assert world.cliff_at(1.9, 1.0) is None
    assert world.remove_cliff("edge") is True
    assert world.cliff_at(2.1, 1.0) is None


def test_visible_entities_carry_distance_and_bearing() -> None:
    world = World()
    world.add_person(Person(id="p1", x_m=1.6, y_m=0.2))
    world.objects["cube"] = WorldObject(id="cube", x_m=1.1, y_m=0.2, label="cube")
    seen = world.visible(0.6, 0.2, 0.0)
    assert [entry["id"] for entry in seen] == ["cube", "p1"]
    assert seen[0]["distance_mm"] == 500
    assert seen[0]["bearing_deg"] == 0


def test_entities_outside_the_field_of_view_are_not_seen() -> None:
    world = World()
    world.add_person(Person(id="behind", x_m=0.1, y_m=0.2))
    assert world.visible(0.6, 0.2, 0.0) == []


def test_the_dock_is_found_by_position_and_bearing() -> None:
    world = World()
    assert world.at_dock(0.2, 0.2) is True
    assert world.at_dock(1.0, 1.0) is False
    bearing = world.dock_bearing(1.2, 0.2, math.pi)
    assert bearing is not None
    distance_m, relative_rad = bearing
    assert distance_m == pytest.approx(1.0)
    assert relative_rad == pytest.approx(0.0, abs=1e-9)


# -- the physics step --------------------------------------------------------------------------


def test_a_move_takes_time_rather_than_teleporting() -> None:
    world = World()
    state = RobotSimState()
    state.refresh_sensors(world)
    action_id, superseded = state.start_move(1000, 200)
    assert superseded is None

    poses: list[int] = []
    finished = []
    for _ in range(60):
        finished = state.step(0.1, world)
        poses.append(state.pose_payload()["x_mm"])
        if finished:
            break
    assert len(poses) == 50  # 1000 mm at 200 mm/s is exactly 5 s at a 100 ms tick
    assert poses[0] == 620 and poses[9] == 800  # 20 mm per tick, from 600 mm
    assert poses == sorted(poses)  # monotonic, never a jump to the end
    assert finished[0].action_id == action_id
    assert finished[0].outcome is MotionOutcome.COMPLETED
    assert state.pose_payload()["x_mm"] == 1600
    assert state.moving is False


def test_a_turn_changes_only_the_heading() -> None:
    world = World()
    state = RobotSimState()
    before = (state.x_m, state.y_m)
    state.start_turn(90, 90)
    for _ in range(20):
        if state.step(0.1, world):
            break
    assert (state.x_m, state.y_m) == before
    assert state.pose_payload()["yaw_deg"] == 90


def test_a_negative_distance_drives_backwards() -> None:
    world = World()
    state = RobotSimState()
    state.start_move(-200, 200)
    for _ in range(20):
        if state.step(0.1, world):
            break
    assert state.pose_payload()["x_mm"] == 400


def test_stop_cancels_the_motion_in_flight() -> None:
    world = World()
    state = RobotSimState()
    action_id, _ = state.start_move(2000, 100)
    state.step(0.5, world)
    result = state.stop()
    assert result is not None
    assert result.action_id == action_id
    assert result.outcome is MotionOutcome.CANCELLED
    assert state.moving is False
    assert state.stop() is None  # idempotent


def test_a_new_move_supersedes_the_one_in_flight() -> None:
    state = RobotSimState()
    first, _ = state.start_move(2000, 100)
    second, superseded = state.start_move(300, 100)
    assert superseded is not None
    assert superseded.action_id == first
    assert superseded.outcome is MotionOutcome.SUPERSEDED
    assert state.action_id == second


def test_an_obstacle_stops_the_move_before_the_bumper_reaches_it() -> None:
    world = World()
    world.add_obstacle(Obstacle(id="box", x_m=1.6, y_m=0.2, radius_m=0.1))
    state = RobotSimState()
    state.start_move(2000, 200)
    result = None
    for _ in range(100):
        finished = state.step(0.1, world)
        if finished:
            result = finished[0]
            break
    assert result is not None
    assert result.outcome is MotionOutcome.OBSTACLE
    assert "box" in result.detail
    assert state.x_m < 1.5 - state.profile.body_radius_m + 0.03


def test_a_cliff_stops_the_move_and_asserts_the_sensors() -> None:
    world = World()
    world.add_cliff(Cliff(id="stairs", x0_m=1.5, y0_m=-1.0, x1_m=1.7, y1_m=1.0))
    state = RobotSimState()
    state.start_move(2000, 200)
    result = None
    for _ in range(100):
        finished = state.step(0.1, world)
        if finished:
            result = finished[0]
            break
    assert result is not None
    assert result.outcome is MotionOutcome.CLIFF
    assert state.cliff_detected is True
    assert state.cliff_payload()["front_left"] is True


def test_a_motor_failure_ends_the_motion() -> None:
    world = World()
    state = RobotSimState()
    state.start_move(2000, 200)
    state.step(0.1, world)
    finished = state.step(0.1, world, motor_failure=True)
    assert finished[0].outcome is MotionOutcome.MOTOR_FAILURE


def test_an_empty_battery_ends_the_motion() -> None:
    world = World()
    state = RobotSimState(battery_pct=0.0)
    state.start_move(2000, 200)
    finished = state.step(0.1, world)
    assert finished[0].outcome is MotionOutcome.BATTERY_EMPTY


def test_the_battery_drains_while_driving_and_charges_on_the_dock() -> None:
    world = World()
    state = RobotSimState(battery_pct=50.0)
    state.start_move(2000, 200)
    for _ in range(10):
        state.step(1.0, world)
    assert state.battery_pct < 50.0
    assert state.charging is False

    parked = RobotSimState(x_m=0.2, y_m=0.2, battery_pct=50.0)
    parked.step(1.0, world)
    assert parked.charging is True
    assert parked.battery_pct > 50.0


def test_sensor_noise_is_seeded_and_therefore_reproducible() -> None:
    world = World()
    readings = []
    for _ in range(2):
        state = RobotSimState(profile=RobotProfile(sensor_noise_mm=5.0))
        state.seed(1234)
        state.refresh_sensors(world)
        readings.append(state.distance_front_mm)
    assert readings[0] == readings[1]

    exact = RobotSimState()
    exact.refresh_sensors(world)
    assert exact.distance_front_mm == 2000  # noise off by default keeps tests exact


# -- the camera ------------------------------------------------------------------------------


def test_a_rendered_frame_is_a_png_the_backend_would_accept() -> None:
    frame = render_frame([])
    assert frame.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(frame) > 100


def test_the_frame_changes_when_the_world_does() -> None:
    empty = render_frame([])
    occupied = render_frame([{"kind": "person", "id": "p1", "label": "p", "distance_mm": 900, "bearing_deg": -15}])
    assert empty != occupied


def test_the_frame_honours_the_requested_size() -> None:
    import struct

    frame = render_frame([], width=64, height=48)
    width, height = struct.unpack(">II", frame[16:24])
    assert (width, height) == (64, 48)
    default = render_frame([])
    assert struct.unpack(">II", default[16:24]) == (DEFAULT_WIDTH, DEFAULT_HEIGHT)


def test_a_fixture_camera_serves_committed_files_in_order(tmp_path: Path) -> None:
    (tmp_path / "b.png").write_bytes(render_frame([]))
    (tmp_path / "a.png").write_bytes(render_frame([], width=32, height=32))
    camera = FixtureCamera(tmp_path)
    assert [camera.capture()[1] for _ in range(3)] == ["a.png", "b.png", "a.png"]


def test_an_empty_fixture_directory_is_a_camera_failure(tmp_path: Path) -> None:
    with pytest.raises(CameraFailure):
        FixtureCamera(tmp_path)


# -- the tool table ----------------------------------------------------------------------------


def test_every_requested_capability_is_published() -> None:
    expected = {
        "robot.get_status",
        "robot.motion.move",
        "robot.motion.turn",
        "robot.motion.stop",
        "robot.follow.target",
        "robot.head.set_angle",
        "robot.head.look_at",
        "robot.lift.set_position",
        "robot.expression.set",
        "robot.animation.play",
        "robot.camera.capture",
        "robot.audio.set_volume",
        "robot.sensor.get_distance",
        "robot.sensor.get_imu",
        "robot.sensor.get_cliff",
        "robot.power.get_battery",
    }
    assert set(TOOL_NAMES) == expected
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=[spec.name for spec in TOOL_SPECS])
def test_no_tool_parameter_names_a_raw_actuator(spec: Any) -> None:
    """The mechanical form of the design rule: the LLM never reaches a motor."""
    haystack = " ".join([*spec.properties, spec.description]).lower()
    for banned in ACTUATOR_DENYLIST:
        assert banned not in haystack, f"{spec.name} mentions {banned}"


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=[spec.name for spec in TOOL_SPECS])
def test_no_tool_parameter_is_a_float(spec: Any) -> None:
    """The firmware vocabulary has no float type, so a `number` parameter is a latent bug."""
    for name, schema in spec.properties.items():
        assert schema["type"] in {"integer", "string", "boolean"}, f"{spec.name}.{name} is {schema['type']}"
        assert schema["description"].strip(), f"{spec.name}.{name} has no description"


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=[spec.name for spec in TOOL_SPECS])
def test_every_definition_is_a_well_formed_mcp_tool(spec: Any) -> None:
    definition = spec.definition()
    assert definition["name"] == spec.name
    assert definition["description"]
    schema = definition["inputSchema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) <= set(schema["properties"])


async def test_status_tool_reports_the_whole_robot() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.get_status"].handler(robot, {})
    payload = json.loads(reply.to_result()["content"][0]["text"])
    assert set(payload) >= {"pose", "motion", "battery", "sensors", "cliff", "imu", "head", "lift", "activity"}


async def test_move_tool_returns_immediately_with_an_action_id() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.motion.move"].handler(robot, {"distance_mm": 400, "speed_mmps": 200})
    payload = reply.payload
    assert payload["accepted"] is True
    assert payload["state"] == "moving"
    assert payload["eta_ms"] == 2000
    assert robot.state.action_id == payload["action_id"]


async def test_move_tool_clamps_out_of_range_arguments() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.motion.move"].handler(robot, {"distance_mm": 99999, "speed_mmps": 99999})
    assert reply.payload["distance_mm"] == 2000
    assert reply.payload["speed_mmps"] == robot.state.profile.max_speed_mmps


async def test_move_tool_rejects_a_zero_distance() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.motion.move"].handler(robot, {"distance_mm": 0})
    assert reply.is_error is True
    assert reply.to_result()["isError"] is True


async def test_head_angles_are_clamped_to_the_mechanical_range() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.head.set_angle"].handler(robot, {"pitch_deg": 900, "yaw_deg": -900})
    assert reply.payload["pitch_deg"] == robot.state.profile.head_pitch_max_deg
    assert reply.payload["yaw_deg"] == -robot.state.profile.head_yaw_limit_deg


async def test_look_at_maps_frame_percentages_onto_head_angles() -> None:
    robot = offline()
    handler = TOOLS_BY_NAME["robot.head.look_at"].handler
    centre = await handler(robot, {"x_pct": 50, "y_pct": 50})
    assert (centre.payload["yaw_deg"], centre.payload["pitch_deg"]) == (0, 0)
    left = await handler(robot, {"x_pct": 0, "y_pct": 50})
    assert left.payload["yaw_deg"] > 0  # the left of the frame is a positive (left) yaw
    down = await handler(robot, {"x_pct": 50, "y_pct": 100})
    assert down.payload["pitch_deg"] < left.payload["pitch_deg"]


async def test_expression_and_animation_land_on_the_state() -> None:
    robot = offline()
    await TOOLS_BY_NAME["robot.expression.set"].handler(robot, {"emotion": "happy", "intensity_pct": 250})
    assert robot.state.emotion == "happy"
    assert robot.state.emotion_intensity_pct == 100
    await TOOLS_BY_NAME["robot.animation.play"].handler(robot, {"name": "greet", "duration_ms": 1500})
    assert robot.state.animation == "greet"
    assert robot.state.animation_remaining_s == pytest.approx(1.5)
    blank = await TOOLS_BY_NAME["robot.animation.play"].handler(robot, {"name": "  "})
    assert blank.is_error is True


async def test_sensor_tools_read_the_world() -> None:
    robot = offline()
    robot.world.add_obstacle(Obstacle(id="box", x_m=1.6, y_m=0.2, radius_m=0.1))
    distance = await TOOLS_BY_NAME["robot.sensor.get_distance"].handler(robot, {})
    assert distance.payload["front_mm"] == 900
    robot.world.add_cliff(Cliff(id="edge", x0_m=0.6, y0_m=0.0, x1_m=0.8, y1_m=0.4))
    cliff = await TOOLS_BY_NAME["robot.sensor.get_cliff"].handler(robot, {})
    assert cliff.payload["detected"] is True
    imu = await TOOLS_BY_NAME["robot.sensor.get_imu"].handler(robot, {})
    assert imu.payload["accel_mg"]["z"] == 1000


async def test_battery_tool_reports_where_the_dock_is() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.power.get_battery"].handler(robot, {})
    assert reply.payload["percent"] > 0
    assert reply.payload["dock_distance_mm"] == 400


async def test_camera_tool_returns_decodable_image_data() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.camera.capture"].handler(robot, {})
    image = base64.b64decode(reply.payload["image_base64"])
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert reply.payload["image_bytes"] == len(image)
    content = reply.to_result()["content"]
    assert content[1]["type"] == "image"
    assert content[1]["mimeType"] == "image/png"


async def test_camera_failure_is_reported_as_a_tool_error() -> None:
    robot = offline()
    robot.faults.camera_failure = True
    reply = await TOOLS_BY_NAME["robot.camera.capture"].handler(robot, {})
    assert reply.is_error is True
    assert "camera failure" in reply.payload["error"]


async def test_a_question_without_a_vision_endpoint_says_so() -> None:
    robot = offline()
    reply = await TOOLS_BY_NAME["robot.camera.capture"].handler(robot, {"question": "who is there?"})
    assert "no vision endpoint" in reply.payload["vision_answer"]


# -- scenarios ---------------------------------------------------------------------------------


def test_every_requested_scenario_exists() -> None:
    assert {
        "person_enters_room",
        "person_leaves_room",
        "robot_gets_bored",
        "battery_low",
        "obstacle_during_move",
        "cliff_during_move",
        "charger_found",
    } <= set(BUILTIN_SCENARIOS)


@pytest.mark.parametrize("name", sorted(BUILTIN_SCENARIOS))
def test_every_builtin_scenario_uses_known_steps(name: str) -> None:
    scenario = get_scenario(name)
    assert scenario.duration_s > 0
    assert scenario.description
    for step in scenario.steps:
        assert step.do in STEP_ACTIONS


def test_get_scenario_hands_back_a_copy() -> None:
    first = get_scenario("idle")
    first.duration_s = 999.0
    assert get_scenario("idle").duration_s != 999.0
    with pytest.raises(KeyError):
        get_scenario("no-such-scenario")


async def test_scenario_steps_fire_once_in_time_order() -> None:
    robot = offline()
    scenario = Scenario(
        name="t",
        steps=[
            Step(at_s=2.0, do="spawn_person", args={"id": "p1", "x_mm": 1200, "y_mm": 200}),
            Step(at_s=1.0, do="set_expression", args={"emotion": "happy"}),
        ],
    )
    runner = ScenarioRunner(scenario, robot)
    await runner.advance_to(0.5)
    assert runner.history == ()
    await runner.advance_to(1.5)
    assert robot.state.emotion == "happy"
    assert len(runner.history) == 1
    await runner.advance_to(5.0)
    assert "p1" in robot.world.people
    assert len(runner.history) == 2
    assert runner.done is True
    await runner.advance_to(50.0)
    assert len(runner.history) == 2  # no step runs twice


async def test_an_unknown_step_is_skipped_not_fatal() -> None:
    robot = offline()
    runner = ScenarioRunner(Scenario(name="t", steps=[Step(at_s=0.0, do="teleport")]), robot)
    await runner.advance_to(1.0)
    assert runner.history == ()


async def test_a_failing_step_is_skipped_not_fatal() -> None:
    robot = offline()
    runner = ScenarioRunner(Scenario(name="t", steps=[Step(at_s=0.0, do="fault", args={"nope": 1})]), robot)
    await runner.advance_to(1.0)
    assert runner.history == ()


async def test_a_fault_step_changes_the_fault_set() -> None:
    robot = offline()
    runner = ScenarioRunner(
        Scenario(name="t", steps=[Step(at_s=1.0, do="fault", args={"motor_failure": True, "packet_delay_ms": 50})]),
        robot,
    )
    await runner.advance_to(1.0)
    assert robot.faults.motor_failure is True
    assert robot.faults.packet_delay_ms == 50


def test_a_scenario_file_loads_with_the_same_shape(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text(
        "duration_s: 12\n"
        "start_x_mm: 800\n"
        "faults:\n"
        "  camera_failure: true\n"
        "steps:\n"
        "  - at_s: 1\n"
        "    do: move\n"
        "    args: {distance_mm: 300}\n",
        encoding="utf-8",
    )
    scenario = load_scenario_file(path)
    assert scenario.name == "custom"
    assert scenario.duration_s == 12
    assert scenario.start_x_mm == 800
    assert scenario.faults.camera_failure is True
    assert scenario.steps[0].do == "move"


def test_a_scenario_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_scenario_file(path)


def test_the_start_pose_is_applied_to_the_state() -> None:
    state = RobotSimState()
    get_scenario("charger_found").apply_start(state)
    assert state.pose_payload() == {"x_mm": 1400, "y_mm": 200, "yaw_deg": 180, "frame": "odom"}
    assert state.battery_pct == 15.0


# -- a whole run, deterministically ---------------------------------------------------------


async def _replay(scenario_name: str, seconds: float) -> tuple[list[str], dict[str, Any]]:
    """Run a scenario on a manual clock and return the notifications and the final pose."""
    robot = offline(get_scenario(scenario_name))
    await drive(robot, seconds)
    return [method for method, _ in robot.notifications], robot.state.pose_payload()


async def test_the_same_scenario_replays_identically() -> None:
    first = await _replay("obstacle_during_move", 12.0)
    second = await _replay("obstacle_during_move", 12.0)
    assert first == second


async def test_an_obstacle_scenario_reports_a_failed_motion() -> None:
    methods, pose = await _replay("obstacle_during_move", 12.0)
    assert "notifications/motion_failed" in methods
    assert "notifications/telemetry" in methods
    assert "notifications/pose" in methods
    assert pose["x_mm"] < 1800


async def test_the_charger_scenario_ends_on_the_dock() -> None:
    robot = offline(get_scenario("charger_found"))
    await drive(robot, 25.0)
    assert robot.world.at_dock(robot.state.x_m, robot.state.y_m) is True
    assert robot.state.charging is True
    assert robot.state.battery_pct > 15.0
    assert "notifications/battery" in [method for method, _ in robot.notifications]


async def test_dropped_notifications_keep_the_run_going() -> None:
    robot = offline()
    robot.faults.drop_notifications = True
    await drive(robot, 3.0)
    assert robot.notifications == ()
    assert robot.state.battery_pct < 85.0  # the robot still lived through the time


def test_the_status_payload_names_everything_a_human_needs() -> None:
    robot = offline()
    status = robot.status()
    assert status["robot_id"] == robot.config.robot_id
    assert status["connected"] is False
    assert len(status["tools"]) == 16
    assert set(status["robot"]) >= {"pose", "battery", "sensors"}
    assert status["scenario"]["name"] == "idle"
    assert "camera_failure" in status["faults"]
