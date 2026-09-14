"""The endpoints that move a robot: what they accept, what refuses them, and why.

This is the file that has to be right. It is the first surface in the project that can
actuate from outside the action layer's own callers, so every gate in front of it is
asserted here rather than assumed: the token, the loopback rule, the rate limit, the
safety policy, and the emergency-stop latch.

The robot underneath is the simulator's own physics and sensor model
(``tests/robot/simulated.py``), so "the robot did not move" is a claim about a pose, not
about a mock.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from robot.api.security import ApiSecurity, RateLimiter  # noqa: E402
from robot.api.server import build_app  # noqa: E402
from robot.runtime import RobotRuntime  # noqa: E402
from robot.simulator.world import Cliff  # noqa: E402
from tests.robot.conftest import ROBOT_ID, until  # noqa: E402
from tests.robot.simulated import SimulatedDevice  # noqa: E402

TOKEN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = f"/api/robots/{ROBOT_ID}"


async def _client(runtime, security):
    async def memories(robot_id: str):
        return None

    client = TestClient(TestServer(build_app(runtime, memories, security=security)))
    await client.start_server()
    return client


@pytest.fixture
async def control():
    """A live API over a runtime with a real simulated robot behind it."""
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    client = await _client(runtime, ApiSecurity(token=TOKEN, host="127.0.0.1"))
    try:
        yield client, runtime, device
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()


# -- the happy path ------------------------------------------------------------------------------


async def test_a_move_is_accepted_and_the_robot_actually_moves(control):
    client, _, device = control
    start_x = device.state.x_m

    response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 250}, headers=AUTH)

    assert response.status == 202, await response.text()
    body = await response.json()
    assert body["type"] == "move"
    assert body["parameters"]["distance_mm"] == 250
    assert await until(lambda: device.state.x_m > start_x + 0.2, timeout=5.0)


async def test_a_turn_and_a_stop_are_accepted(control):
    client, _, device = control
    assert (await client.post(f"{BASE}/actions/turn", json={"angle_deg": 90}, headers=AUTH)).status == 202
    stop = await client.post(f"{BASE}/actions/stop", json={}, headers=AUTH)
    assert stop.status == 202
    assert await until(lambda: not device.state.moving, timeout=3.0)


async def test_the_head_and_the_lift_move(control):
    client, _, device = control
    assert (await client.post(f"{BASE}/actions/head", json={"pitch_deg": 10}, headers=AUTH)).status == 202
    assert (await client.post(f"{BASE}/actions/lift", json={"height_pct": 40}, headers=AUTH)).status == 202
    assert await until(lambda: device.state.head_pitch_deg == 10, timeout=3.0)
    assert await until(lambda: device.state.lift_pct == 40, timeout=3.0)


async def test_an_expression_and_an_animation_are_accepted(control):
    client, _, device = control
    assert (await client.post(f"{BASE}/expression", json={"emotion": "happy"}, headers=AUTH)).status == 202
    assert await until(lambda: device.state.emotion == "happy", timeout=3.0)

    animations = await (await client.get(f"{BASE}/animations", headers=AUTH)).json()
    assert animations["animations"], "the robot published no animations"
    played = await client.post(
        f"{BASE}/animations/{animations['animations'][0]}", json={}, headers=AUTH
    )
    assert played.status == 202


async def test_the_autonomy_mode_can_be_changed(control):
    client, runtime, _ = control
    response = await client.post(f"{BASE}/autonomy-mode", json={"mode": "passive"}, headers=AUTH)
    assert response.status == 200
    assert (await response.json())["mode"] == "passive"
    assert runtime.autonomy.value == "passive"


# -- validation ----------------------------------------------------------------------------------


async def test_a_missing_field_is_a_400(control):
    client, _, _ = control
    response = await client.post(f"{BASE}/actions/move", json={}, headers=AUTH)
    assert response.status == 400
    assert "distance_mm" in (await response.json())["error"]


async def test_an_invented_field_is_a_400_not_a_silent_default(control):
    client, _, device = control
    response = await client.post(
        f"{BASE}/actions/move", json={"distance_mm": 100, "velocity": 9}, headers=AUTH
    )
    assert response.status == 400
    assert device.called("robot.motion.move") == ()


async def test_a_float_distance_is_refused(control):
    client, _, _ = control
    response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 100.5}, headers=AUTH)
    assert response.status == 400


async def test_an_unknown_autonomy_mode_is_a_400(control):
    client, _, _ = control
    response = await client.post(f"{BASE}/autonomy-mode", json={"mode": "berserk"}, headers=AUTH)
    assert response.status == 400
    assert "berserk" in (await response.json())["error"]


async def test_a_body_that_is_not_json_is_a_400(control):
    client, _, _ = control
    response = await client.post(
        f"{BASE}/actions/move", data="not json", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert response.status == 400


async def test_an_unknown_robot_is_a_404(control):
    client, _, _ = control
    response = await client.post("/api/robots/nobody/actions/move", json={"distance_mm": 100}, headers=AUTH)
    assert response.status == 404


# -- safety --------------------------------------------------------------------------------------


async def test_an_unsafe_move_is_a_409_with_the_typed_reason(control):
    """The robot is at the edge of a drop. The API is told no, in the policy's own words."""
    client, _, device = control
    device.world.add_cliff(Cliff(id="edge", x0_m=0.62, y0_m=-0.4, x1_m=1.4, y1_m=0.8))
    device.state.refresh_sensors(device.world)
    await device.step(0.0)
    assert await until(lambda: device.state.cliff_detected, timeout=2.0)
    start_x = device.state.x_m

    response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 250}, headers=AUTH)

    assert response.status == 409
    assert (await response.json())["reason"] == "cliff_hazard"
    assert device.called("robot.motion.move") == ()
    assert device.state.x_m == pytest.approx(start_x, abs=1e-6)


async def test_a_distance_past_the_configured_limit_is_a_409(control):
    client, _, _ = control
    response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 99_999}, headers=AUTH)
    assert response.status == 409
    assert (await response.json())["reason"] == "distance_limit_exceeded"


async def test_the_emergency_stop_latches_and_refuses_everything_after_it(control):
    client, _, device = control
    engaged = await client.post(f"{BASE}/emergency-stop", json={}, headers=AUTH)
    assert engaged.status == 202
    body = await engaged.json()
    assert body["engaged"] is True
    assert "watchdog" in body["note"], "the response implies a guarantee the backend cannot make"

    refused = await client.post(f"{BASE}/actions/move", json={"distance_mm": 100}, headers=AUTH)
    assert refused.status == 409
    assert (await refused.json())["reason"] == "emergency_stop_engaged"

    detail = await (await client.get(BASE, headers=AUTH)).json()
    assert detail["emergency_stopped"] is True


async def test_the_emergency_stop_can_be_cleared_and_the_robot_moves_again(control):
    client, _, device = control
    await client.post(f"{BASE}/emergency-stop", json={}, headers=AUTH)
    cleared = await client.delete(f"{BASE}/emergency-stop", headers=AUTH)

    assert cleared.status == 200
    assert (await cleared.json())["cleared"] is True
    assert (await client.post(f"{BASE}/actions/move", json={"distance_mm": 100}, headers=AUTH)).status == 202


async def test_everything_in_flight_can_be_cancelled(control):
    client, _, device = control
    await client.post(f"{BASE}/actions/move", json={"distance_mm": 900, "speed_mmps": 40}, headers=AUTH)
    assert await until(lambda: device.state.moving, timeout=3.0)

    response = await client.delete(f"{BASE}/actions", headers=AUTH)

    assert response.status == 200
    assert (await response.json())["cancelled"] >= 1
    assert await until(lambda: not device.state.moving, timeout=3.0)


# -- the gates ------------------------------------------------------------------------------------


async def test_control_needs_the_token(control):
    client, _, device = control
    response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 100})
    assert response.status == 401
    assert device.called("robot.motion.move") == ()


async def test_control_is_refused_off_loopback_even_with_the_right_token():
    """A token in a shell history is a token. A robot on a network needs a second gate."""
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    client = await _client(runtime, ApiSecurity(token=TOKEN, host="0.0.0.0"))
    try:
        response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 100}, headers=AUTH)
        assert response.status == 403
        assert "loopback" in (await response.json())["error"]
        # Reading is unaffected: telemetry from a laptop is not the thing that needs a gate.
        assert (await client.get(f"{BASE}/state", headers=AUTH)).status == 200
        assert device.called("robot.motion.move") == ()
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()


async def test_control_off_loopback_is_allowed_when_it_is_explicitly_turned_on():
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    security = ApiSecurity(token=TOKEN, host="0.0.0.0", allow_remote_control=True)
    client = await _client(runtime, security)
    try:
        response = await client.post(f"{BASE}/actions/move", json={"distance_mm": 100}, headers=AUTH)
        assert response.status == 202
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()


async def test_control_is_rate_limited():
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    security = ApiSecurity(token=TOKEN, rate_limiter=RateLimiter(limit=3, window_s=60.0))
    client = await _client(runtime, security)
    try:
        statuses = [
            (await client.post(f"{BASE}/actions/stop", json={}, headers=AUTH)).status for _ in range(5)
        ]
        assert statuses[:3] == [202, 202, 202]
        assert statuses[3:] == [429, 429]
        # Reading is not rate limited: a dashboard polls, and that is not the hazard.
        assert (await client.get(f"{BASE}/state", headers=AUTH)).status == 200
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()


# -- injection ------------------------------------------------------------------------------------


async def test_an_injected_cliff_refuses_the_next_move(control):
    """The injection goes in the same door a real sensor does, so the policy reads it.

    The simulated device's ticker is stopped first, because a device that keeps reporting
    overwrites an injection on its next frame — it is telling the truth and the injection
    is not. Injection is for sensors a device does not report.
    """
    client, _, device = control
    await device.aclose()

    injected = await client.post(f"{BASE}/simulate/cliff", json={"detected": True}, headers=AUTH)
    assert injected.status == 202

    refused = await client.post(f"{BASE}/actions/move", json={"distance_mm": 200}, headers=AUTH)
    assert refused.status == 409
    assert (await refused.json())["reason"] == "cliff_hazard"


async def test_an_injected_person_reaches_the_world_model(control):
    client, runtime, _ = control

    await client.post(f"{BASE}/simulate/person", json={"person_id": "ahmad"}, headers=AUTH)
    await runtime.events.drain()

    world = await (await client.get(f"{BASE}/world", headers=AUTH)).json()
    assert any(entity["type"] == "person" for entity in world["entities"])


async def test_an_injected_low_battery_shows_up_in_state(control):
    client, _, _ = control
    await client.post(f"{BASE}/simulate/low_battery", json={"percent": 4}, headers=AUTH)
    state = await (await client.get(f"{BASE}/state", headers=AUTH)).json()
    assert state["battery"]["percent"] == 4


async def test_an_unknown_injection_is_a_400(control):
    client, _, _ = control
    response = await client.post(f"{BASE}/simulate/earthquake", json={}, headers=AUTH)
    assert response.status == 400


async def test_injection_is_treated_as_control_not_as_a_read():
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)
    client = await _client(runtime, ApiSecurity(token=TOKEN, host="0.0.0.0"))
    try:
        response = await client.post(f"{BASE}/simulate/obstacle", json={"detected": False}, headers=AUTH)
        assert response.status == 403
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()
