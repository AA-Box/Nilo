"""``robot/telemetry.py``: device notifications turned into world state.

Parsing is a pure function over a dict, so most of this file needs no runtime, no socket
and no event loop. The two ingestion tests use the per-test ``runtime`` fixture; the
end-to-end path (a real device sending these frames over a real socket) is
``tests/integration/test_simulator_e2e.py``.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from robot.events.types import MotionCompleted, MotionFailed, RobotEvent, TelemetryUpdated
from robot.runtime import RobotRuntime
from robot.session import attach_connection, handle_notification
from robot.state.models import RobotActivity, RobotTelemetry
from robot.telemetry import TELEMETRY_METHODS, ingest, is_telemetry, parse
from tests.robot.conftest import FakeConnection, device_info

POSE = {"x_mm": 1250, "y_mm": -300, "yaw_deg": 90, "frame": "odom"}
BATTERY = {"percent": 41, "charging": False, "battery_mv": 3690}


def test_the_claimed_methods_are_the_documented_ones() -> None:
    assert "notifications/telemetry" in TELEMETRY_METHODS
    assert {
        "notifications/pose",
        "notifications/battery",
        "notifications/sensor",
        "notifications/motion_completed",
        "notifications/motion_failed",
    } <= TELEMETRY_METHODS
    assert is_telemetry("notifications/telemetry") is True
    assert is_telemetry("notifications/something-else") is False
    assert is_telemetry(None) is False
    assert is_telemetry("tools/call") is False


def test_a_method_we_do_not_own_is_not_parsed() -> None:
    assert parse("r1", "tools/list", {}) is None


def test_pose_is_converted_from_millimetres_and_degrees() -> None:
    parsed = parse("r1", "notifications/pose", POSE)
    assert parsed is not None
    pose = parsed.telemetry.pose
    assert pose is not None
    assert pose.x_m == pytest.approx(1.25)
    assert pose.y_m == pytest.approx(-0.3)
    assert pose.theta_rad == pytest.approx(math.pi / 2)
    assert pose.frame == "odom"


def test_battery_millivolts_become_volts() -> None:
    parsed = parse("r1", "notifications/battery", BATTERY)
    assert parsed is not None
    battery = parsed.telemetry.battery
    assert battery is not None
    assert battery.percent == 41
    assert battery.voltage_v == pytest.approx(3.69)
    assert battery.is_low is False


def test_a_battery_percentage_outside_the_range_is_clamped() -> None:
    parsed = parse("r1", "notifications/battery", {"percent": 250})
    assert parsed is not None and parsed.telemetry.battery is not None
    assert parsed.telemetry.battery.percent == 100
    assert parsed.telemetry.battery.voltage_v is None


def test_motion_speeds_are_converted_to_si_units() -> None:
    parsed = parse(
        "r1",
        "notifications/motion",
        {"moving": True, "action_id": "abc", "linear_speed_mmps": 200, "angular_speed_mdps": 90000},
    )
    assert parsed is not None and parsed.telemetry.motion is not None
    motion = parsed.telemetry.motion
    assert motion.moving is True
    assert motion.action_id == "abc"
    assert motion.linear_speed_mps == pytest.approx(0.2)
    assert motion.angular_speed_rps == pytest.approx(math.radians(90))


def test_sensor_readings_keep_only_numbers() -> None:
    parsed = parse(
        "r1",
        "notifications/sensor",
        {
            "cliff_detected": True,
            "bump_detected": False,
            "readings": {"front_mm": 120, "label": "front", "flag": True},
        },
    )
    assert parsed is not None and parsed.telemetry.sensors is not None
    sensors = parsed.telemetry.sensors
    assert sensors.cliff_detected is True
    assert sensors.blocked is True
    assert sensors.readings == {"front_mm": 120.0}


def test_expression_intensity_is_a_fraction() -> None:
    parsed = parse("r1", "notifications/expression", {"emotion": "happy", "intensity_pct": 80, "animation": "greet"})
    assert parsed is not None and parsed.telemetry.expression is not None
    assert parsed.telemetry.expression.intensity == pytest.approx(0.8)
    assert parsed.telemetry.expression.animation == "greet"


def test_an_unknown_activity_is_recorded_as_idle() -> None:
    parsed = parse("r1", "notifications/activity", {"activity": "brooding"})
    assert parsed is not None and parsed.telemetry.activity is not None
    assert parsed.telemetry.activity.activity is RobotActivity.IDLE
    known = parse("r1", "notifications/activity", {"activity": "charging", "detail": "on the dock"})
    assert known is not None and known.telemetry.activity is not None
    assert known.telemetry.activity.activity is RobotActivity.CHARGING
    assert known.telemetry.activity.detail == "on the dock"


def test_a_composite_frame_fills_in_every_slice() -> None:
    parsed = parse(
        "r1",
        "notifications/telemetry",
        {
            "pose": POSE,
            "battery": BATTERY,
            "motion": {"moving": False},
            "sensors": {"picked_up": True},
            "audio": {"volume_pct": 70, "listening": True},
            "vision": {"camera_active": True, "faces_detected": 2},
            "expression": {"emotion": "curious"},
            "activity": {"activity": "idle"},
        },
    )
    assert parsed is not None
    assert parsed.telemetry.changed_fields(RobotTelemetry()) == (
        "pose",
        "motion",
        "battery",
        "sensors",
        "audio",
        "vision",
        "expression",
        "activity",
    )
    assert parsed.telemetry.audio is not None and parsed.telemetry.audio.volume_percent == 70
    assert parsed.telemetry.vision is not None and parsed.telemetry.vision.faces_detected == 2


def test_an_empty_frame_is_parsed_and_reported_as_empty() -> None:
    parsed = parse("r1", "notifications/telemetry", {})
    assert parsed is not None
    assert parsed.empty is True


@pytest.mark.parametrize(
    "payload",
    [
        {"x_mm": "not-a-number", "yaw_deg": None},
        {"x_mm": True},
        {},
        {"frame": 7},
    ],
)
def test_a_malformed_field_falls_back_instead_of_raising(payload: dict[str, Any]) -> None:
    parsed = parse("r1", "notifications/pose", payload)
    assert parsed is not None and parsed.telemetry.pose is not None
    assert parsed.telemetry.pose.frame == "odom"


def test_motion_completed_carries_the_action_and_the_pose() -> None:
    parsed = parse("r1", "notifications/motion_completed", {"action_id": "a1", "kind": "move", "pose": POSE})
    assert parsed is not None
    event = parsed.event
    assert isinstance(event, MotionCompleted)
    assert event.action_id == "a1"
    assert event.kind == "move"
    assert event.pose is not None and event.pose.x_m == pytest.approx(1.25)


def test_motion_failed_carries_the_reason() -> None:
    parsed = parse(
        "r1",
        "notifications/motion_failed",
        {"action_id": "a2", "kind": "turn", "reason": "cliff", "detail": "cliff sensor stairs asserted"},
    )
    assert parsed is not None
    event = parsed.event
    assert isinstance(event, MotionFailed)
    assert (event.action_id, event.kind, event.reason) == ("a2", "turn", "cliff")
    assert "stairs" in event.detail


def test_a_motion_notification_without_an_action_id_produces_no_event() -> None:
    parsed = parse("r1", "notifications/motion_completed", {"pose": POSE})
    assert parsed is not None
    assert parsed.event is None
    assert parsed.telemetry.pose is not None  # the pose still lands


# -- ingestion -------------------------------------------------------------------------------


async def test_ingest_updates_state_and_publishes_events(runtime: RobotRuntime) -> None:
    await runtime.attach(device_info(), None)
    seen: list[RobotEvent] = []
    runtime.events.subscribe(RobotEvent, seen.append)

    assert await ingest(runtime, "aa-bb-cc-dd-ee-ff", "notifications/battery", BATTERY) is True
    assert await ingest(
        runtime, "aa-bb-cc-dd-ee-ff", "notifications/motion_completed", {"action_id": "a1", "pose": POSE}
    ) is True
    await runtime.events.drain()

    state = await runtime.registry.get("aa-bb-cc-dd-ee-ff")
    assert state is not None
    assert state.telemetry.battery is not None and state.telemetry.battery.percent == 41
    assert state.telemetry.pose is not None and state.telemetry.pose.x_m == pytest.approx(1.25)
    assert any(isinstance(event, MotionCompleted) for event in seen)
    assert any(isinstance(event, TelemetryUpdated) for event in seen)


async def test_ingest_ignores_a_method_it_does_not_own(runtime: RobotRuntime) -> None:
    await runtime.attach(device_info(), None)
    assert await ingest(runtime, "aa-bb-cc-dd-ee-ff", "notifications/log", {"message": "hi"}) is False


async def test_ingest_for_an_unregistered_robot_does_not_raise(runtime: RobotRuntime) -> None:
    assert await ingest(runtime, "ghost", "notifications/battery", BATTERY) is True
    assert await runtime.registry.get("ghost") is None


async def test_the_session_seam_claims_only_telemetry(runtime: RobotRuntime) -> None:
    conn = FakeConnection(features={"mcp": False})
    session = await attach_connection(conn, runtime)
    assert session is not None

    payload = {"jsonrpc": "2.0", "method": "notifications/battery", "params": BATTERY}
    assert await handle_notification(conn, payload, runtime) is True
    assert await handle_notification(conn, {"jsonrpc": "2.0", "method": "roots/list"}, runtime) is False

    state = await runtime.registry.get(session.robot_id)
    assert state is not None and state.telemetry.battery is not None
    assert state.telemetry.battery.percent == 41


async def test_the_session_seam_ignores_an_unattached_connection(runtime: RobotRuntime) -> None:
    conn = FakeConnection()
    payload = {"jsonrpc": "2.0", "method": "notifications/battery", "params": BATTERY}
    assert await handle_notification(conn, payload, runtime) is False


async def test_the_session_seam_never_raises_on_a_hostile_payload(runtime: RobotRuntime) -> None:
    conn = FakeConnection(features={"mcp": False})
    await attach_connection(conn, runtime)
    assert await handle_notification(conn, {"method": "notifications/pose", "params": "not-a-mapping"}, runtime) is True
    assert await handle_notification(conn, {}, runtime) is False
