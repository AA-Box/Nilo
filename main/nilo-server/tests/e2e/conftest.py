"""The end-to-end harness: a real server, a real socket, and four local fakes.

The difference from ``tests/integration`` is the voice half. That suite stubs the
per-connection providers away entirely, because no action test speaks; this one installs
a fake synthesizer and a rule-based model instead, so a scenario can start at "a person
said something" and end at "the robot moved and answered".
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("websockets")
pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from config.opus_loader import setup_opus  # noqa: E402

setup_opus()

import core.connection as connection  # noqa: E402
import core.websocket_server as websocket_server  # noqa: E402
from config.config_loader import load_config  # noqa: E402
from core.utils.cache.config import CacheType  # noqa: E402
from core.utils.cache.manager import cache_manager  # noqa: E402
from robot.events.types import RobotEvent  # noqa: E402
from robot.runtime import RobotRuntime, set_runtime  # noqa: E402
from robot.simulator import Scenario, SimulatedRobot, SimulatorConfig, World  # noqa: E402
from robot.simulator.clock import RealClock  # noqa: E402
from robot.state.models import normalize_robot_id  # noqa: E402
from tests.e2e.fakes import FakeTTS, RuleBasedLLM, install_fake_tts  # noqa: E402
from tests.e2e.transcript import Report, Transcript  # noqa: E402
from tests.integration.conftest import TEST_DISCOVERY_TIMEOUT, free_port  # noqa: E402

ROBOT_ID = "aa:bb:cc:e2:e0:01"
NORMALIZED = normalize_robot_id(ROBOT_ID)

#: Simulated seconds per real second. A five-second move is a quarter of a second of test
#: time while the *server* still sees real network timing.
CLOCK_SPEED = 20.0

#: Where the run's report lands. Git-ignored; CI keeps it as an artifact.
REPORT_PATH = Path(__file__).resolve().parents[2] / "tmp" / "e2e-report.md"


@dataclass
class E2EBackend:
    """A running server, the robot control plane behind it, and the fakes."""

    config: dict[str, Any]
    runtime: RobotRuntime
    llm: RuleBasedLLM
    ws_url: str
    port: int
    connections: list[Any] = field(default_factory=list)
    tts: list[FakeTTS] = field(default_factory=list)

    async def state(self, robot_id: str = NORMALIZED) -> Any:
        return await self.runtime.registry.get(robot_id)

    def connection(self) -> Any:
        """The newest live device connection. Handy for the two tests that need one."""
        return self.connections[-1] if self.connections else None

    def speech(self) -> FakeTTS | None:
        return self.tts[-1] if self.tts else None

    async def wait_for(
        self, predicate: Callable[[], Any], *, timeout: float = 15.0, interval: float = 0.02
    ) -> Any:
        """Poll until a predicate returns something truthy, and return it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: Any = None
        while loop.time() < deadline:
            last = predicate()
            if asyncio.iscoroutine(last):
                last = await last
            if last:
                return last
            await asyncio.sleep(interval)
        raise AssertionError(f"condition not met within {timeout}s (last value: {last!r})")


@pytest.fixture(scope="session")
def report() -> Iterator[Report]:
    """Collects every scenario's transcript and writes the run's report once."""
    collected = Report(REPORT_PATH)
    yield collected
    if collected.transcripts:
        collected.write()


@pytest.fixture
async def backend(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[E2EBackend]:
    """A nilo-server on an ephemeral port, with a rule-based model and a fake synthesizer."""
    fakes = RuleBasedLLM()
    built = E2EBackend(
        config={}, runtime=RobotRuntime(), llm=fakes, ws_url="", port=0
    )  # replaced below; declared here so the patch can close over it

    def initialize(self: Any) -> None:
        """What ``_initialize_components`` does in this suite: a synthesizer, nothing else.

        The real method constructs a VAD, an ASR, a TTS, a memory and an intent provider
        per session. None of those is reachable in CI, and a suite that loads them climbs
        a gigabyte per test.
        """
        built.connections.append(self)
        built.tts.append(install_fake_tts(self))

    monkeypatch.setattr(websocket_server, "initialize_modules", lambda *args, **kwargs: {})
    monkeypatch.setattr(connection.ConnectionHandler, "_initialize_components", initialize)

    cache_manager.delete(CacheType.CONFIG, "main_config")
    config = await load_config()
    port = free_port()
    config["server"]["ip"] = "127.0.0.1"
    config["server"]["port"] = port
    config["server"]["auth_key"] = "e2e-test-key"
    config["server"].setdefault("auth", {})["enabled"] = False

    runtime = RobotRuntime(discovery_timeout=TEST_DISCOVERY_TIMEOUT, llm=fakes)
    set_runtime(runtime)
    built.config = config
    built.runtime = runtime
    built.ws_url = f"ws://127.0.0.1:{port}/nilo/v1/"
    built.port = port

    server = websocket_server.WebSocketServer(config)
    task = asyncio.create_task(server.start(), name="e2e-ws-server")
    await _wait_until_listening(port)
    try:
        yield built
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await runtime.aclose()
        set_runtime(None)
        cache_manager.delete(CacheType.CONFIG, "main_config")


@pytest.fixture
def scenario(backend: E2EBackend, report: Report) -> Callable[[str, str], Transcript]:
    """Start recording a scenario's transitions. Call it once, at the top of a test."""

    def start(name: str, description: str = "") -> Transcript:
        transcript = Transcript(name=name, description=description)
        backend.runtime.events.subscribe(RobotEvent, transcript)
        report.add(transcript)
        return transcript

    return start


def make_robot(
    backend: E2EBackend,
    *,
    scenario: Scenario | None = None,
    robot_id: str = ROBOT_ID,
    speed: float = CLOCK_SPEED,
    **options: Any,
) -> SimulatedRobot:
    """A simulator pointed at the test server, with the status endpoint off by default."""
    defaults: dict[str, Any] = {
        "tick_ms": 50,
        "telemetry_ms": 200,
        "pose_ms": 100,
        "status_port": 0,
        "reconnect": False,
    }
    settings = SimulatorConfig(server_url=backend.ws_url, robot_id=robot_id, **{**defaults, **options})
    chosen = scenario or Scenario(name="e2e", duration_s=0.0)
    return SimulatedRobot(settings, scenario=chosen, clock=RealClock(speed), world=World(chosen.world))


@contextlib.asynccontextmanager
async def running(robot: SimulatedRobot) -> AsyncIterator[SimulatedRobot]:
    """Run a simulator for the duration of a block, and tear it down whatever happens."""
    task = asyncio.create_task(robot.run(), name="e2e-simulator")
    try:
        await robot.wait_connected(timeout=20)
        yield robot
    finally:
        robot.stop()
        await robot.drop_connection(reconnect=False)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest.fixture
async def robot(backend: E2EBackend) -> AsyncIterator[SimulatedRobot]:
    """A connected, discovered simulator whose telemetry has reached the world model."""
    async with running(make_robot(backend)) as simulator:
        await simulator.wait_discovered(timeout=20)
        await backend.wait_for(lambda: _ready(backend))
        yield simulator


async def _ready(backend: E2EBackend) -> Any:
    """Registered, discovered, and with a sensor picture the safety policy can read."""
    state = await backend.state()
    if state is None or state.telemetry.sensors is None:
        return None
    return state if state.capabilities.has_tool("robot_motion_move") else None


async def _wait_until_listening(port: int, *, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise AssertionError(f"the server did not start listening on {port} within {timeout}s")
