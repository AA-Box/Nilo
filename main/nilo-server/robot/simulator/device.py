"""The simulated device: a client that speaks the Nilo session protocol end to end.

It connects to the real server on the real route, sends a real ``hello``, answers the
device-MCP handshake the server starts, serves the tool table from
:mod:`robot.simulator.tools`, and reports telemetry as JSON-RPC notifications. Nothing
here reaches into the backend: if a behaviour cannot be produced by a device on a socket,
the simulator cannot produce it either, which is the whole point of having one
(docs/robot-roadmap.md, "No hardware in CI").

Three loops run concurrently:

* the **tick loop** — physics, the scenario timeline and telemetry, driven by the
  simulated clock. It keeps running across a reconnect, because a robot does not stop
  existing when its uplink drops.
* the **session loop** — connect, handshake, read, and dial again when the link goes.
* the **status server** — a one-route HTTP endpoint so a human can read the robot's own
  state while a scenario runs.

Faults are injected at the transport and tool boundaries. Timing faults (packet delay, a
tool that never answers) use *real* time, not simulated time: they exist to trip the
server's real timeouts, which an accelerated clock would otherwise scale away.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from robot import __version__
from robot.simulator.camera import CameraFailure, FixtureCamera, render_frame
from robot.simulator.clock import Clock, RealClock
from robot.simulator.scenarios import Faults, Scenario, ScenarioRunner, get_scenario
from robot.simulator.state import MotionOutcome, MotionResult, RobotProfile, RobotSimState
from robot.simulator.tools import TOOL_SPECS, TOOLS_BY_NAME, ToolReply
from robot.simulator.world import World, WorldSpec

logger = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "ws://127.0.0.1:8000/nilo/v1/"
MCP_PROTOCOL_VERSION = "2024-11-05"
#: Audio parameters the device advertises. Matches what the inherited handler expects.
AUDIO_PARAMS: dict[str, Any] = {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}


@dataclass
class SimulatorConfig:
    """Everything a simulator run needs that is not the scenario."""

    server_url: str = DEFAULT_SERVER_URL
    robot_id: str = "nilo-sim-01"
    client_id: str = "nilo-simulator"
    token: str | None = None
    hardware_model: str = "nilo-simulator"
    firmware_version: str = __version__
    tick_ms: int = 100
    telemetry_ms: int = 1000
    pose_ms: int = 250
    #: Tools per ``tools/list`` page. 0 publishes them all on one page.
    tools_page_size: int = 0
    #: Stop after this much simulated time. 0 runs until interrupted.
    duration_s: float = 0.0
    reconnect: bool = True
    reconnect_delay_s: float = 1.0
    #: TCP port for the read-only status endpoint. 0 disables it.
    status_port: int = 8090
    status_host: str = "127.0.0.1"
    camera_fixtures: str | None = None
    camera_width: int = 160
    camera_height: int = 120
    vision_timeout_s: float = 10.0
    profile: RobotProfile = field(default_factory=RobotProfile)
    seed: int = 0


class SimulatedRobot:
    """One simulated robot, on one session at a time."""

    def __init__(
        self,
        config: SimulatorConfig | None = None,
        *,
        scenario: Scenario | None = None,
        clock: Clock | None = None,
        world: World | None = None,
    ) -> None:
        self.config = config or SimulatorConfig()
        self.scenario = scenario or get_scenario("idle")
        self.clock: Clock = clock or RealClock()
        spec: WorldSpec | None = self.scenario.world or (world.spec if world is not None else None)
        self.world = world if world is not None else World(spec)
        self.state = RobotSimState(profile=self.config.profile)
        self.state.seed(self.config.seed)
        self.scenario.apply_start(self.state)
        self.state.refresh_sensors(self.world)
        self.faults: Faults = self.scenario.faults.model_copy(deep=True)
        self.runner = ScenarioRunner(self.scenario, self)

        self._ws: Any = None
        self._session_id: str | None = None
        self._vision: tuple[str, str] | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._welcomed = asyncio.Event()
        self._motion_results: list[MotionResult] = []
        self._sent_notifications: list[tuple[str, dict[str, Any]]] = []
        self._fixtures: FixtureCamera | None = (
            FixtureCamera(self.config.camera_fixtures) if self.config.camera_fixtures else None
        )
        self._tool_calls: list[tuple[str, dict[str, Any]]] = []
        self._call_tasks: set[asyncio.Task[None]] = set()
        self._disconnect_fired = False
        self._last_battery_pct = int(round(self.state.battery_pct))
        self._last_sensor_flags = self._sensor_flags()
        self._status_server: asyncio.Server | None = None
        self._discovered = asyncio.Event()
        self._next_telemetry = 0.0
        self._next_pose = 0.0
        # Simulated time the scenario timeline is measured from, or None while it is
        # still waiting to be anchored. See _scenario_time.
        self._scenario_epoch: float | None = None
        self._expects_server = False

    # -- introspection, for tests and the status endpoint ---------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def tool_calls(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return tuple(self._tool_calls)

    @property
    def notifications(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        return tuple(self._sent_notifications)

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """Wait until the server has answered ``hello``, not merely until the socket opened."""
        await asyncio.wait_for(self._welcomed.wait(), timeout=timeout)

    async def wait_discovered(self, timeout: float = 15.0) -> None:
        """Wait until the server has finished ``tools/list``. Proves discovery happened."""
        await asyncio.wait_for(self._discovered.wait(), timeout=timeout)

    def status(self) -> dict[str, Any]:
        """The robot's own view of itself: what ``--status`` prints."""
        return {
            "robot_id": self.config.robot_id,
            "server_url": self.config.server_url,
            "connected": self.connected,
            "session_id": self._session_id,
            "sim_time_s": round(self.clock.now(), 3),
            "clock_speed": self.clock.speed,
            "scenario": {
                "name": self.scenario.name,
                "duration_s": self.scenario.duration_s,
                "started": self._scenario_epoch is not None,
                "steps_done": len(self.runner.history),
                "history": [{"at_s": round(at, 2), "step": what} for at, what in self.runner.history],
            },
            "faults": self.faults.model_dump(),
            "tools": [spec.name for spec in TOOL_SPECS],
            "tool_calls": [{"name": name, "arguments": args} for name, args in self._tool_calls[-20:]],
            "notifications_sent": len(self._sent_notifications),
            "robot": self.state.snapshot(self.world),
        }

    # -- the ScenarioHost / ToolHost surface ---------------------------------------------

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

    def _record(self, result: MotionResult | None) -> None:
        if result is not None:
            self._motion_results.append(result)

    async def drop_connection(self, *, reconnect: bool = True) -> None:
        """Kill the socket the way a robot losing power does: no close frame."""
        self.config.reconnect = reconnect
        websocket, self._ws = self._ws, None
        self._connected.clear()
        self._welcomed.clear()
        if websocket is None:
            return
        logger.warning("simulator %s: dropping the connection (reconnect=%s)", self.config.robot_id, reconnect)
        transport = getattr(websocket, "transport", None)
        if transport is not None:
            transport.abort()
        else:  # pragma: no cover - every websockets connection has a transport
            with contextlib.suppress(Exception):
                await websocket.close()

    async def capture_frame(self, question: str | None) -> dict[str, Any]:
        """One camera frame, plus the vision endpoint's answer when asked for one."""
        if self.faults.camera_failure:
            raise CameraFailure("injected camera failure")
        if self._fixtures is not None:
            image, source = self._fixtures.capture()
            mime_type = "image/jpeg" if source.lower().endswith((".jpg", ".jpeg")) else "image/png"
        else:
            image = render_frame(
                self.world.visible(self.state.x_m, self.state.y_m, self.state.yaw_rad),
                width=self.config.camera_width,
                height=self.config.camera_height,
                fov_deg=self.world.spec.camera_fov_deg,
                cliff=self.state.cliff_detected,
            )
            source, mime_type = "synthetic", "image/png"
        frame: dict[str, Any] = {
            "source": source,
            "mime_type": mime_type,
            "width": self.config.camera_width,
            "height": self.config.camera_height,
            "captured_at_s": round(self.clock.now(), 3),
            "visible": self.world.visible(self.state.x_m, self.state.y_m, self.state.yaw_rad),
            "image_base64": base64.b64encode(image).decode("ascii"),
        }
        if question and self._vision is not None:
            frame["vision_answer"] = await self._ask_vision(question, image, mime_type)
        elif question:
            frame["vision_answer"] = "no vision endpoint was offered during the MCP handshake"
        return frame

    async def _ask_vision(self, question: str, image: bytes, mime_type: str) -> str:
        if self._vision is None:  # pragma: no cover - the caller checks first
            return "no vision endpoint was offered during the MCP handshake"
        url, token = self._vision
        try:
            return await asyncio.to_thread(
                _post_vision,
                url,
                token,
                self.config.robot_id,
                question,
                image,
                mime_type,
                self.config.vision_timeout_s,
            )
        except Exception as exc:
            logger.warning("simulator %s: vision upload failed: %s", self.config.robot_id, exc)
            return f"vision upload failed: {exc}"

    # -- running --------------------------------------------------------------------------

    async def run(self) -> None:
        """Tick, connect, serve, reconnect — until the duration elapses or stop() is called."""
        await self._start_status_server()
        # There is a server to talk to, so the scenario waits for it (see _scenario_time).
        self._expects_server = True
        tick = asyncio.create_task(self._tick_loop(), name="simulator-tick")
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("simulator %s: session ended: %s", self.config.robot_id, exc)
                self._ws = None
                self._connected.clear()
                self._welcomed.clear()
                self._discovered.clear()
                if self._stop.is_set() or not self.config.reconnect:
                    break
                logger.info("simulator %s: reconnecting in %.1fs", self.config.robot_id, self.config.reconnect_delay_s)
                await asyncio.sleep(self.config.reconnect_delay_s)
        finally:
            self._expects_server = False
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
            for task in list(self._call_tasks):
                task.cancel()
            await self._stop_status_server()

    def stop(self) -> None:
        self._stop.set()

    async def _session(self) -> None:
        import websockets

        headers = {"device-id": self.config.robot_id, "client-id": self.config.client_id}
        if self.config.token:
            headers["authorization"] = f"Bearer {self.config.token}"
        logger.info("simulator %s: connecting to %s", self.config.robot_id, self.config.server_url)
        async with websockets.connect(
            self.config.server_url, additional_headers=headers, ping_interval=None, open_timeout=10
        ) as websocket:
            self._ws = websocket
            self._connected.set()
            await self._send_hello()
            async for raw in websocket:
                if self._stop.is_set():
                    break
                await self._on_message(raw)

    async def _send_hello(self) -> None:
        await self._send(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "device_id": self.config.robot_id,
                "features": {"mcp": True},
                "audio_params": dict(AUDIO_PARAMS),
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        websocket = self._ws
        if websocket is None:
            return
        if self.faults.packet_delay_ms:
            # Real seconds on purpose: this fault exists to trip the server's real timeouts.
            await asyncio.sleep(self.faults.packet_delay_ms / 1000.0)
        try:
            await websocket.send(json.dumps(message))
        except Exception as exc:
            logger.warning("simulator %s: send failed: %s", self.config.robot_id, exc)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.faults.drop_notifications:
            return
        self._sent_notifications.append((method, params))
        await self._send({"type": "mcp", "payload": {"jsonrpc": "2.0", "method": method, "params": params}})

    # -- inbound --------------------------------------------------------------------------

    async def _on_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            return  # server audio; a simulator with no speaker drops it
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("simulator %s: non-JSON frame ignored", self.config.robot_id)
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "hello":
            self._session_id = message.get("session_id")
            self._welcomed.set()
            logger.info("simulator %s: session %s established", self.config.robot_id, self._session_id)
        elif kind == "mcp":
            payload = message.get("payload")
            if isinstance(payload, dict):
                await self._on_mcp(payload)
        elif kind == "tts":
            state = message.get("state")
            self.state.speaking = state in {"start", "sentence_start"}
        elif kind == "goodbye":
            self.stop()
        else:
            logger.debug("simulator %s: ignoring %s frame", self.config.robot_id, kind)

    async def _on_mcp(self, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        if method is None:
            return  # a response to a request the simulator never sends
        message_id = payload.get("id")
        params = _mapping(payload.get("params"))
        if method == "initialize":
            await self._on_initialize(message_id, params)
        elif method == "tools/list":
            await self._on_tools_list(message_id, params)
        elif method == "tools/call":
            self._spawn_call(message_id, params)
        elif method == "ping":
            await self._reply(message_id, {})
        else:
            logger.debug("simulator %s: unhandled MCP method %s", self.config.robot_id, method)

    async def _on_initialize(self, message_id: Any, params: dict[str, Any]) -> None:
        vision = _mapping(params.get("capabilities")).get("vision")
        if isinstance(vision, dict) and vision.get("url") and vision.get("token"):
            self._vision = (str(vision["url"]), str(vision["token"]))
            logger.info("simulator %s: vision endpoint offered at %s", self.config.robot_id, vision["url"])
        await self._reply(
            message_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.config.hardware_model, "version": self.config.firmware_version},
            },
        )

    async def _on_tools_list(self, message_id: Any, params: dict[str, Any]) -> None:
        page_size = self.config.tools_page_size or len(TOOL_SPECS)
        cursor = params.get("cursor")
        start = 0
        if isinstance(cursor, str) and cursor.startswith("page-"):
            with contextlib.suppress(ValueError):
                start = int(cursor.removeprefix("page-"))
        page = TOOL_SPECS[start : start + page_size]
        result: dict[str, Any] = {"tools": [spec.definition() for spec in page]}
        end = start + page_size
        if end < len(TOOL_SPECS):
            result["nextCursor"] = f"page-{end}"
        else:
            self._discovered.set()
        await self._reply(message_id, result)

    def _spawn_call(self, message_id: Any, params: dict[str, Any]) -> None:
        """Answer a tool call off the read loop, so a slow tool cannot stall ingestion."""
        task = asyncio.create_task(self._on_tools_call(message_id, params), name="simulator-tool-call")
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)

    async def _on_tools_call(self, message_id: Any, params: dict[str, Any]) -> None:
        name = str(params.get("name", ""))
        arguments = _mapping(params.get("arguments"))
        self._tool_calls.append((name, dict(arguments)))
        if self.faults.timing_out(name):
            logger.warning("simulator %s: swallowing the call to %s (injected timeout)", self.config.robot_id, name)
            return
        if self.faults.erroring(name):
            await self._reply(message_id, {"isError": True, "error": f"injected failure in {name}"})
            return
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            await self._reply_error(message_id, -32601, f"unknown tool {name!r}")
            return
        try:
            reply: ToolReply = await spec.handler(self, dict(arguments))
        except Exception as exc:
            logger.warning("simulator %s: tool %s raised: %s", self.config.robot_id, name, exc)
            await self._reply(message_id, {"isError": True, "error": f"{type(exc).__name__}: {exc}"})
            return
        await self._reply(message_id, reply.to_result())

    async def _reply(self, message_id: Any, result: dict[str, Any]) -> None:
        await self._send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": message_id, "result": result}})

    async def _reply_error(self, message_id: Any, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        await self._send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": message_id, "error": error}})

    # -- the tick loop --------------------------------------------------------------------

    async def step(self, dt_s: float) -> None:
        """One tick: physics, the scenario timeline, and whatever that has to report.

        Public because it is the whole simulator minus the socket: a test drives it with a
        :class:`~robot.simulator.clock.ManualClock` and gets a byte-identical run every time.
        """
        now = self.clock.now()
        scenario_now = self._scenario_time(now)
        self._motion_results.extend(self.state.step(dt_s, self.world, motor_failure=self.faults.motor_failure))
        if scenario_now is not None:
            await self._check_disconnect_fault(scenario_now)
            await self.runner.advance_to(scenario_now)
        await self._drain_motion_results()
        await self._notify_changes()
        if now >= self._next_telemetry:
            self._next_telemetry = now + self.config.telemetry_ms / 1000.0
            await self._notify("notifications/telemetry", self.state.telemetry_payload(self.world))
        if self.state.moving and now >= self._next_pose:
            self._next_pose = now + self.config.pose_ms / 1000.0
            await self._notify("notifications/pose", self.state.pose_payload())

    def _scenario_time(self, now: float) -> float | None:
        """Scenario time, or ``None`` while the timeline is still waiting to start.

        The timeline cannot run from the start of the process. A scripted run on an
        accelerated clock reaches ``at_s=2`` a quarter of a second in, which on a slow
        machine is still inside the WebSocket handshake and the MCP discovery — so the
        step fires into a socket nobody is listening on, its effects are never reported,
        and a test waiting for them waits forever. That is a race whose outcome depends on
        how busy the host is, which is the one thing a simulator exists to remove.

        So: when there is a server to talk to, the timeline starts when the server has
        finished discovering this robot, and a reconnect does not restart it (a scenario
        is a story, not a loop). With no server — an offline test driving :meth:`step` by
        hand — it starts at zero, exactly as simulated time does.
        """
        if self._scenario_epoch is None:
            if not self._expects_server:
                self._scenario_epoch = 0.0
            elif self._discovered.is_set():
                self._scenario_epoch = now
                logger.info(
                    "simulator %s: scenario %s starts now (t=%.2fs, discovery complete)",
                    self.config.robot_id,
                    self.scenario.name,
                    now,
                )
            else:
                return None
        return now - self._scenario_epoch

    async def _tick_loop(self) -> None:
        dt = self.config.tick_ms / 1000.0
        while not self._stop.is_set():
            await self.clock.sleep(dt)
            await self.step(dt)
            # Against scenario time, so a slow handshake does not eat the run's duration.
            elapsed = self._scenario_time(self.clock.now())
            now = 0.0 if elapsed is None else elapsed
            if self.config.duration_s and now >= self.config.duration_s:
                logger.info("simulator %s: duration reached at t=%.1fs", self.config.robot_id, now)
                self.stop()
                await self._close_gracefully()
                return

    async def _check_disconnect_fault(self, now_s: float) -> None:
        at = self.faults.disconnect_at_s
        if at is not None and not self._disconnect_fired and now_s >= at:
            self._disconnect_fired = True
            await self.drop_connection(reconnect=self.faults.reconnect)

    async def _drain_motion_results(self) -> None:
        results, self._motion_results = self._motion_results, []
        if self.faults.drop_motion_completion:
            # The motion still happens; the server is simply never told it ended. This is
            # what a firmware bug or a lost frame looks like from the backend, and it is
            # the only way to exercise the action watchdog on a live, healthy session.
            if results:
                logger.warning(
                    "simulator %s: swallowing %d motion completion(s) (injected fault)",
                    self.config.robot_id,
                    len(results),
                )
            return
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
        percent = int(round(self.state.battery_pct))
        if percent != self._last_battery_pct:
            self._last_battery_pct = percent
            await self._notify("notifications/battery", self.state.battery_payload())
        flags = self._sensor_flags()
        if flags != self._last_sensor_flags:
            self._last_sensor_flags = flags
            await self._notify("notifications/sensor", self.state.sensor_payload())

    def _sensor_flags(self) -> tuple[bool, bool, bool]:
        return (self.state.cliff_detected, self.state.bump, self.state.picked_up)

    async def _close_gracefully(self) -> None:
        websocket, self._ws = self._ws, None
        self._connected.clear()
        if websocket is None:
            return
        with contextlib.suppress(Exception):
            await websocket.send(json.dumps({"type": "goodbye", "session_id": self._session_id}))
        with contextlib.suppress(Exception):
            await websocket.close()

    # -- the status endpoint ---------------------------------------------------------------

    async def _start_status_server(self) -> None:
        if not self.config.status_port:
            return
        try:
            self._status_server = await asyncio.start_server(
                self._serve_status, self.config.status_host, self.config.status_port
            )
        except OSError as exc:
            logger.warning("simulator %s: status endpoint unavailable: %s", self.config.robot_id, exc)
            return
        logger.info(
            "simulator %s: state at http://%s:%d/state",
            self.config.robot_id,
            self.config.status_host,
            self.config.status_port,
        )

    async def _stop_status_server(self) -> None:
        server, self._status_server = self._status_server, None
        if server is None:
            return
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def _serve_status(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            with contextlib.suppress(asyncio.IncompleteReadError, TimeoutError):
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            body = json.dumps(self.status(), indent=2, default=str).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception as exc:  # a status reader must never take the simulator down
            logger.debug("simulator %s: status request failed: %s", self.config.robot_id, exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _mapping(value: Any) -> dict[str, Any]:
    """``value`` as a string-keyed dict, or an empty one. Wire data is never trusted."""
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def fetch_status(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    """Read a running simulator's status endpoint. Used by ``--status``."""
    with urllib.request.urlopen(f"http://{host}:{port}/state", timeout=timeout) as response:
        payload: Any = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise ValueError("the status endpoint did not return an object")
    return payload


def _post_vision(
    url: str, token: str, device_id: str, question: str, image: bytes, mime_type: str, timeout: float
) -> str:
    """POST one frame to the server's vision endpoint, the way firmware does.

    Hand-rolled multipart rather than a new HTTP dependency: the endpoint reads exactly two
    parts, in order (``core/api/vision_handler.py``).
    """
    boundary = f"----nilo-sim-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"question\"\r\n\r\n{question}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"frame.png\"\r\n"
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        image,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        url,
        data=b"".join(parts),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
            "Device-Id": device_id,
            "Client-Id": device_id,
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return f"vision endpoint returned HTTP {exc.code}"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(body, dict) and body.get("success"):
        return str(body.get("response", ""))
    detail = body.get("message", body) if isinstance(body, dict) else body
    return f"vision endpoint rejected the frame after {elapsed_ms} ms: {detail}"
