"""A real simulated robot behind a tool channel, with no socket in the middle.

``robot/simulator/`` is a WebSocket client: it connects to a running server, completes the
handshake, and speaks device MCP over the wire. That is the right thing for an integration
test and the wrong thing for this package, where no test opens a socket.

So this harness keeps everything about the simulator that matters — the physics step, the
sensor model, the world, the published tool table, the motion-completion notifications —
and replaces only the transport. Tool calls arrive through
:class:`~robot.devices.tools.ToolChannel` and notifications go straight into
:func:`robot.telemetry.ingest`, which is exactly where they would have arrived from a
socket.

    device = SimulatedDevice()
    await device.attach(runtime)          # registers the robot and starts ticking
    ...
    await device.aclose()

The robot it simulates is the real one: ``robot.motion.move`` starts a motion that takes
time, the pose integrates, an obstacle stops it early, and the completion notification
that settles the action comes back on its own.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from robot.devices.mcp import sanitize_tool_name, tool_from_mcp
from robot.simulator.scenarios import Faults
from robot.simulator.state import MotionOutcome, MotionResult, RobotProfile, RobotSimState
from robot.simulator.tools import TOOLS_BY_NAME, ToolReply
from robot.simulator.world import World, WorldSpec
from robot.state.models import DeviceInfo, RobotCapabilities
from robot.telemetry import ingest

logger = logging.getLogger(__name__)

#: How often the simulated robot steps, in seconds. Short: a 250 mm move at 200 mm/s takes
#: 1.25 s of simulated time, and a test should not wait a whole tick to see it finish.
TICK_S = 0.01


class SimulatedDevice:
    """The simulator's brain and body, on a tool channel instead of a WebSocket."""

    def __init__(
        self,
        robot_id: str = "test-robot",
        *,
        world: World | None = None,
        profile: RobotProfile | None = None,
        seed: int = 7,
    ) -> None:
        self.robot_id = robot_id
        self.world = world if world is not None else World(WorldSpec())
        self.state = RobotSimState(profile=profile or RobotProfile())
        self.state.seed(seed)
        self.state.refresh_sensors(self.world)
        self.faults = Faults()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._motion_results: list[MotionResult] = []
        self._runtime: Any = None
        self._ticker: asyncio.Task[None] | None = None
        self._last_flags = self._sensor_flags()

    # -- the ToolHost surface the simulator's own handlers expect -----------------------------

    def command_move(self, distance_mm: int, speed_mmps: int) -> str:
        action_id, superseded = self.state.start_move(distance_mm, speed_mmps)
        self._record(superseded)
        return action_id

    def command_turn(self, angle_deg: int, speed_dps: int) -> str:
        action_id, superseded = self.state.start_turn(angle_deg, speed_dps)
        self._record(superseded)
        return action_id

    def command_follow(self, target_id: str, duration_ms: int, stop_distance_mm: int) -> str:
        action_id, superseded = self.state.start_follow(target_id, duration_ms, stop_distance_mm)
        self._record(superseded)
        return action_id

    def command_stop(self) -> None:
        self._record(self.state.stop())

    async def capture_frame(self, question: str | None) -> dict[str, Any]:
        """A frame without the pixels: the camera itself is not what these tests are about."""
        seen = self.world.visible(self.state.x_m, self.state.y_m, self.state.yaw_rad)
        labels = sorted({str(item.get("label") or item.get("kind")) for item in seen})
        return {
            "captured": True,
            "answer": ", ".join(labels) or "nothing in particular",
            "objects": len(seen),
        }

    def _record(self, result: MotionResult | None) -> None:
        if result is not None:
            self._motion_results.append(result)

    # -- the ToolChannel surface the runtime expects --------------------------------------------

    def capabilities(self) -> RobotCapabilities:
        return RobotCapabilities(
            mcp=True,
            tools=tuple(tool_from_mcp(spec.definition()) for spec in TOOLS_BY_NAME.values()),
        )

    async def discover(self) -> RobotCapabilities:
        return self.capabilities()

    async def call_tool(self, name: str, arguments: Any = None, *, timeout: float | None = None) -> str:
        """Dispatch to the simulator's own handler, by the raw dotted tool name."""
        raw = self._raw_name(name)
        self.calls.append((raw, dict(arguments or {})))
        spec = TOOLS_BY_NAME.get(raw)
        if spec is None:
            raise LookupError(f"the simulated robot publishes no tool named {name!r}")
        reply: ToolReply = await spec.handler(self, dict(arguments or {}))
        result = reply.to_result()
        if result.get("isError"):
            raise RuntimeError(str(result.get("error", "the simulated robot refused")))
        return json.dumps(reply.payload)

    async def aclose(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker

    # -- running ------------------------------------------------------------------------------------

    async def attach(self, runtime: Any, *, tick_s: float = TICK_S) -> SimulatedDevice:
        """Register with a runtime and start stepping. Returns self, for one-line setup."""
        self._runtime = runtime
        await runtime.attach(
            DeviceInfo(device_id=self.robot_id, session_id="session-1", features={"mcp": True}),
            self,
            discover=False,
        )
        await runtime.registry.set_capabilities(self.robot_id, self.capabilities())
        await self.step(0.0)
        self._ticker = asyncio.create_task(self._tick(tick_s), name=f"simulated-{self.robot_id}")
        return self

    async def step(self, dt_s: float) -> None:
        """One tick: physics, then whatever that has to report to the backend."""
        self._motion_results.extend(self.state.step(dt_s, self.world))
        await self._drain_motion_results()
        await self._notify_changes()
        await self._notify("notifications/telemetry", self.state.telemetry_payload(self.world))

    def called(self, tool_name: str) -> tuple[dict[str, Any], ...]:
        return tuple(args for name, args in self.calls if name == tool_name)

    # -- internals --------------------------------------------------------------------------------------

    async def _tick(self, tick_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(tick_s)
                await self.step(tick_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("the simulated robot stopped ticking")

    async def _drain_motion_results(self) -> None:
        results, self._motion_results = self._motion_results, []
        for result in results:
            params: dict[str, Any] = {
                "action_id": result.action_id,
                "kind": result.kind.value,
                "pose": self.state.pose_payload(),
                "motion": self.state.motion_payload(),
            }
            if result.succeeded:
                await self._notify("notifications/motion_completed", params)
                continue
            params["reason"] = result.outcome.value
            params["detail"] = result.detail
            if result.outcome in {MotionOutcome.CLIFF, MotionOutcome.OBSTACLE}:
                params["sensors"] = self.state.sensor_payload()
            await self._notify("notifications/motion_failed", params)

    async def _notify_changes(self) -> None:
        flags = self._sensor_flags()
        if flags != self._last_flags:
            self._last_flags = flags
            await self._notify("notifications/sensor", self.state.sensor_payload())

    def _sensor_flags(self) -> tuple[bool, bool, bool]:
        return (self.state.cliff_detected, self.state.bump, self.state.picked_up)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._runtime is None:
            return
        await ingest(self._runtime, self.robot_id, method, params)

    @staticmethod
    def _raw_name(name: str) -> str:
        """The dotted name the simulator published, from the sanitized one the server uses."""
        if name in TOOLS_BY_NAME:
            return name
        for raw in TOOLS_BY_NAME:
            if sanitize_tool_name(raw) == name:
                return raw
        return name


__all__ = ["TICK_S", "SimulatedDevice"]
