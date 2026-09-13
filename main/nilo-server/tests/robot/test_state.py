"""Domain models, freshness and the state store."""
from __future__ import annotations

from datetime import timedelta

import pytest

from robot.state import (
    ConnectionStatus,
    DeviceInfo,
    InMemoryRobotStateStore,
    RobotBatteryState,
    RobotCapabilities,
    RobotPose,
    RobotSensorState,
    RobotState,
    RobotTelemetry,
    RobotTool,
    StaleStateError,
    normalize_robot_id,
    utcnow,
)


def _state(robot_id: str = "aa-bb") -> RobotState:
    device = DeviceInfo(device_id="AA:BB", session_id="s1")
    return RobotState(robot_id=robot_id, identity=device.identity(), connection=device.connection())


@pytest.mark.parametrize(
    ("device_id", "expected"),
    [("AA:BB:CC", "aa-bb-cc"), ("aa-bb-cc", "aa-bb-cc"), (" AA_BB ", "aa-bb"), ("robot01", "robot01")],
)
def test_robot_id_is_stable_across_spellings(device_id, expected):
    assert normalize_robot_id(device_id) == expected


def test_robot_id_rejects_an_unusable_device_id():
    with pytest.raises(ValueError):
        normalize_robot_id("::::")


def test_models_are_frozen():
    pose = RobotPose(x_m=1.0)
    with pytest.raises(Exception):
        pose.x_m = 2.0


def test_state_entries_carry_an_age():
    pose = RobotPose(updated_at=utcnow() - timedelta(seconds=5))
    assert 4.9 < pose.age_seconds() < 6.0
    assert pose.is_fresh(10.0)
    assert not pose.is_fresh(1.0)


def test_reading_a_stale_entry_raises_rather_than_returning_stale_data():
    pose = RobotPose(updated_at=utcnow() - timedelta(seconds=2))
    assert pose.require_fresh(5.0) is pose
    with pytest.raises(StaleStateError) as excinfo:
        pose.require_fresh(0.5)
    assert excinfo.value.max_age_s == 0.5
    assert excinfo.value.age_s > 0.5


def test_battery_percent_is_bounded():
    with pytest.raises(ValueError):
        RobotBatteryState(percent=140)
    assert RobotBatteryState(percent=12).is_low
    assert not RobotBatteryState(percent=12, charging=True).is_low


def test_sensor_state_knows_when_driving_is_unsafe():
    assert not RobotSensorState().blocked
    assert RobotSensorState(cliff_detected=True).blocked
    assert RobotSensorState(picked_up=True).blocked


def test_telemetry_merge_only_overlays_reported_fields():
    base = RobotTelemetry(pose=RobotPose(x_m=1.0), battery=RobotBatteryState(percent=80))
    merged = base.merge(RobotTelemetry(battery=RobotBatteryState(percent=75)))
    assert merged.pose is not None and merged.pose.x_m == 1.0
    assert merged.battery is not None and merged.battery.percent == 75
    assert merged.changed_fields(base) == ("battery",)


def test_empty_telemetry_patch_is_a_no_op():
    base = RobotTelemetry(pose=RobotPose(x_m=1.0))
    assert base.merge(RobotTelemetry()) is base


def test_capabilities_are_a_discovered_tool_set():
    tool = RobotTool(name="self_battery", raw_name="self.battery", description="battery level")
    capabilities = RobotCapabilities(mcp=True, tools=(tool,))
    assert capabilities.has_tool("self_battery")
    assert capabilities.get_tool("self_battery") is tool
    assert capabilities.get_tool("nope") is None
    assert capabilities.tool_names == ("self_battery",)
    assert not RobotCapabilities().has_tool("self_battery")


def test_device_info_builds_identity_and_connection_consistently():
    device = DeviceInfo(device_id="AA:BB", session_id="s1", features={"mcp": True, "aec": False})
    identity, connection = device.identity(), device.connection()
    assert identity.robot_id == connection.robot_id == "aa-bb"
    assert device.supports_mcp
    assert connection.status is ConnectionStatus.CONNECTED
    assert connection.is_connected


async def test_store_round_trip():
    store = InMemoryRobotStateStore()
    state = _state()
    assert await store.get("aa-bb") is None
    await store.put(state)
    assert (await store.get("aa-bb")) == state
    assert await store.ids() == ["aa-bb"]
    assert await store.delete("aa-bb")
    assert not await store.delete("aa-bb")
    assert await store.list_states() == []


async def test_store_mutate_is_read_modify_write():
    store = InMemoryRobotStateStore()
    await store.put(_state())
    updated = await store.mutate(
        "aa-bb", lambda state: state.model_copy(update={"telemetry": RobotTelemetry(pose=RobotPose(x_m=3.0))})
    )
    assert updated is not None and updated.telemetry.pose is not None
    assert updated.telemetry.pose.x_m == 3.0
    assert await store.mutate("missing", lambda state: state) is None
