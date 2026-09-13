"""The robot registry: registration, reconnect, duplicates, state updates, teardown."""
from __future__ import annotations

import asyncio

import pytest

from robot.devices import RobotCapabilityRegistry, RobotRegistry
from robot.events import (
    BatteryUpdated,
    CapabilitiesRefreshed,
    EventBus,
    PoseUpdated,
    RobotConnected,
    RobotDisconnected,
    RobotEvent,
    SensorUpdated,
    TelemetryUpdated,
    ToolDiscovered,
)
from robot.state import (
    ConnectionStatus,
    DeviceInfo,
    DisconnectReason,
    InMemoryRobotStateStore,
    RobotBatteryState,
    RobotCapabilities,
    RobotPose,
    RobotSensorState,
    RobotTelemetry,
    RobotTool,
)


class Recorder:
    """Collects every event the registry publishes, in order."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: list[RobotEvent] = []
        bus.subscribe(RobotEvent, self.events.append)

    async def settled(self) -> list[RobotEvent]:
        await self.bus.drain()
        return self.events

    async def of(self, event_type: type[RobotEvent]) -> list[RobotEvent]:
        await self.bus.drain()
        return [event for event in self.events if isinstance(event, event_type)]


@pytest.fixture
async def bus():
    active = EventBus()
    try:
        yield active
    finally:
        await active.aclose()


@pytest.fixture
def registry(bus):
    return RobotRegistry(InMemoryRobotStateStore(), bus, RobotCapabilityRegistry())


@pytest.fixture
async def recorder(bus):
    # async: subscribing starts a worker task, which needs a running event loop.
    return Recorder(bus)


def device(device_id: str = "AA:BB:CC", session: str = "s1") -> DeviceInfo:
    return DeviceInfo(device_id=device_id, session_id=session, features={"mcp": True})


async def register(registry: RobotRegistry, info: DeviceInfo):
    return await registry.register(info.identity(), info.connection())


async def test_registering_a_robot_stores_it_and_announces_it(registry, recorder):
    state = await register(registry, device())

    assert state.robot_id == "aa-bb-cc"
    assert state.identity.device_id == "AA:BB:CC"
    assert state.connection.status is ConnectionStatus.CONNECTED
    assert state.connection.reconnect_count == 0
    assert await registry.get("aa-bb-cc") == state

    connected = await recorder.of(RobotConnected)
    assert len(connected) == 1
    assert connected[0].robot_id == "aa-bb-cc"
    assert not connected[0].reconnect


async def test_list_returns_connected_robots_only(registry):
    await register(registry, device("AA:BB:CC", "s1"))
    await register(registry, device("11:22:33", "s2"))
    assert [state.robot_id for state in await registry.list()] == ["11-22-33", "aa-bb-cc"]

    await registry.unregister("aa-bb-cc")
    assert [state.robot_id for state in await registry.list()] == ["11-22-33"]
    assert len(await registry.list(connected_only=False)) == 2


async def test_two_devices_are_two_robots(registry):
    await register(registry, device("AA:BB:CC", "s1"))
    await register(registry, device("11:22:33", "s2"))
    assert len(await registry.list()) == 2


async def test_a_second_connection_for_one_device_supersedes_the_first(registry, recorder):
    await register(registry, device(session="s1"))
    state = await register(registry, device(session="s2"))

    assert len(await registry.list()) == 1
    assert state.connection.session_id == "s2"
    assert state.connection.reconnect_count == 1

    disconnects = await recorder.of(RobotDisconnected)
    assert [(event.session_id, event.reason) for event in disconnects] == [("s1", DisconnectReason.SUPERSEDED)]
    connects = await recorder.of(RobotConnected)
    assert [event.reconnect for event in connects] == [False, True]


async def test_re_registering_the_same_session_is_idempotent(registry, recorder):
    await register(registry, device(session="s1"))
    state = await register(registry, device(session="s1"))

    assert state.connection.reconnect_count == 0
    assert await recorder.of(RobotDisconnected) == []


async def test_a_reconnect_keeps_telemetry_and_capabilities_until_discovery_refreshes_them(registry):
    await register(registry, device(session="s1"))
    await registry.update_state("aa-bb-cc", RobotTelemetry(battery=RobotBatteryState(percent=64)))
    tool = RobotTool(name="self_battery", raw_name="self.battery")
    await registry.set_capabilities("aa-bb-cc", RobotCapabilities(mcp=True, tools=(tool,)))

    await registry.unregister("aa-bb-cc")
    state = await register(registry, device(session="s2"))

    assert state.telemetry.battery is not None and state.telemetry.battery.percent == 64
    assert state.identity.capabilities.tool_names == ("self_battery",)
    assert state.connection.reconnect_count == 1
    # the live capability index was cleared on disconnect and is refilled by discovery
    assert await registry.get_capabilities("aa-bb-cc") is None


async def test_unregister_marks_the_robot_disconnected_and_clears_its_capabilities(registry, recorder):
    await register(registry, device())
    await registry.set_capabilities("aa-bb-cc", RobotCapabilities(mcp=True))

    assert await registry.unregister("aa-bb-cc", reason=DisconnectReason.TIMEOUT)

    state = await registry.get("aa-bb-cc")
    assert state is not None
    assert state.connection.status is ConnectionStatus.DISCONNECTED
    assert state.connection.disconnect_reason is DisconnectReason.TIMEOUT
    assert state.connection.disconnected_at is not None
    assert await registry.get_capabilities("aa-bb-cc") is None
    assert await registry.get_tools("aa-bb-cc") is None
    assert [event.reason for event in await recorder.of(RobotDisconnected)] == [DisconnectReason.TIMEOUT]


async def test_disconnecting_twice_is_a_no_op(registry, recorder):
    await register(registry, device())
    assert await registry.unregister("aa-bb-cc")
    assert not await registry.unregister("aa-bb-cc")
    assert not await registry.unregister("who-is-this")
    assert len(await recorder.of(RobotDisconnected)) == 1


async def test_a_late_teardown_from_an_old_session_does_not_unregister_the_new_one(registry):
    await register(registry, device(session="s1"))
    await register(registry, device(session="s2"))

    assert not await registry.unregister("aa-bb-cc", session_id="s1")

    state = await registry.get("aa-bb-cc")
    assert state is not None and state.connection.is_connected
    assert await registry.unregister("aa-bb-cc", session_id="s2")


async def test_update_state_merges_telemetry_and_announces_what_changed(registry, recorder):
    await register(registry, device())

    await registry.update_state("aa-bb-cc", RobotTelemetry(pose=RobotPose(x_m=1.0)))
    state = await registry.update_state("aa-bb-cc", RobotTelemetry(battery=RobotBatteryState(percent=41)))

    assert state is not None
    assert state.telemetry.pose is not None and state.telemetry.pose.x_m == 1.0
    assert state.telemetry.battery is not None and state.telemetry.battery.percent == 41
    assert [event.changed for event in await recorder.of(TelemetryUpdated)] == [("pose",), ("battery",)]
    assert len(await recorder.of(PoseUpdated)) == 1
    assert len(await recorder.of(BatteryUpdated)) == 1


async def test_a_sensor_update_is_announced_on_its_own_channel(registry, recorder):
    await register(registry, device())
    await registry.update_state("aa-bb-cc", RobotTelemetry(sensors=RobotSensorState(cliff_detected=True)))
    sensors = await recorder.of(SensorUpdated)
    assert len(sensors) == 1
    assert sensors[0].sensors.blocked


async def test_an_update_that_changes_nothing_publishes_nothing(registry, recorder):
    await register(registry, device())
    await registry.update_state("aa-bb-cc", RobotTelemetry())
    assert await recorder.of(TelemetryUpdated) == []


async def test_updating_an_unknown_robot_returns_none(registry):
    assert await registry.update_state("ghost", RobotTelemetry(pose=RobotPose())) is None
    assert await registry.update_last_seen("ghost") is None
    assert await registry.get("ghost") is None
    assert await registry.get_capabilities("ghost") is None


async def test_telemetry_and_last_seen_refresh_liveness(registry):
    state = await register(registry, device())
    first_seen = state.connection.last_seen_at

    await asyncio.sleep(0.01)
    bumped = await registry.update_last_seen("aa-bb-cc")
    assert bumped is not None
    assert bumped.connection.last_seen_at > first_seen
    assert bumped.identity.last_seen_at == bumped.connection.last_seen_at

    await asyncio.sleep(0.01)
    after_telemetry = await registry.update_state("aa-bb-cc", RobotTelemetry(pose=RobotPose(x_m=2.0)))
    assert after_telemetry is not None
    assert after_telemetry.connection.last_seen_at > bumped.connection.last_seen_at


async def test_capabilities_are_recorded_announced_and_refreshed(registry, recorder):
    await register(registry, device())
    battery = RobotTool(name="self_battery", raw_name="self.battery")
    move = RobotTool(name="robot_move", raw_name="robot.move")

    await registry.set_capabilities("aa-bb-cc", RobotCapabilities(mcp=True, tools=(battery,)))
    state = await registry.set_capabilities("aa-bb-cc", RobotCapabilities(mcp=True, tools=(battery, move)))

    assert state is not None
    assert state.identity.capabilities.tool_names == ("self_battery", "robot_move")
    capabilities = await registry.get_capabilities("aa-bb-cc")
    assert capabilities is not None and capabilities.has_tool("robot_move")
    tools = await registry.get_tools("aa-bb-cc")
    assert tools is not None and tools.names == ("self_battery", "robot_move")

    discovered = await recorder.of(ToolDiscovered)
    assert [event.tool.name for event in discovered] == ["self_battery", "robot_move"]
    refreshes = await recorder.of(CapabilitiesRefreshed)
    assert [event.refreshed for event in refreshes] == [False, True]


async def test_discovery_that_lands_after_the_robot_is_gone_is_dropped(registry):
    assert await registry.set_capabilities("ghost", RobotCapabilities(mcp=True)) is None


async def test_identity_fields_are_filled_in_from_what_the_device_reported(registry):
    await register(registry, device())
    state = await registry.set_capabilities(
        "aa-bb-cc",
        RobotCapabilities(mcp=True, server_name="little-robot", server_version="3.4.5", protocol_version="2024-11-05"),
    )
    assert state is not None
    assert state.identity.hardware_model == "little-robot"
    assert state.identity.firmware_version == "3.4.5"
    assert state.identity.protocol_version == "2024-11-05"


async def test_concurrent_registration_and_updates_stay_consistent(registry):
    """asyncio concurrency: many robots connecting and reporting at once."""
    devices = [device(f"AA:BB:{index:02d}", f"s{index}") for index in range(25)]
    await asyncio.gather(*(register(registry, info) for info in devices))

    await asyncio.gather(
        *(
            registry.update_state(info.robot_id, RobotTelemetry(battery=RobotBatteryState(percent=index)))
            for index, info in enumerate(devices)
        )
    )

    states = await registry.list()
    assert len(states) == 25
    for index, info in enumerate(devices):
        state = await registry.get(info.robot_id)
        assert state is not None and state.telemetry.battery is not None
        assert state.telemetry.battery.percent == index

    await asyncio.gather(*(registry.unregister(info.robot_id) for info in devices))
    assert await registry.list() == []


async def test_one_device_reconnecting_in_a_loop_never_forks_into_two_robots(registry):
    for index in range(10):
        await register(registry, device(session=f"s{index}"))
        await registry.unregister("aa-bb-cc", session_id=f"s{index}")
    assert len(await registry.list(connected_only=False)) == 1
    state = await registry.get("aa-bb-cc")
    assert state is not None and state.connection.reconnect_count == 9
