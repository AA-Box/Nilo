"""A headless nilo-server, in-process, for the simulator end-to-end tests.

"Headless" is exactly one thing: no VAD, ASR, TTS, LLM, memory or intent provider is
constructed. That takes **two** stubs, and for a while it only had one — see
:func:`backend`. Everything else is the production path — the real
:class:`~core.websocket_server.WebSocketServer`, the real
:class:`~core.connection.ConnectionHandler`, the real ``hello`` handler, the real device
MCP handshake and the real robot seam. A test therefore exercises the code that ships;
what it skips is a torch model download, which no robot test needs.

The robot runtime is installed per test with :func:`robot.runtime.set_runtime`, so two
tests never share a registry, and the server picks it up through the same ``get_runtime()``
the production seam uses.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("websockets")  # full runtime dependency; skipped on the dev-only slice
pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from config.opus_loader import setup_opus  # noqa: E402

setup_opus()

import core.connection as connection  # noqa: E402
import core.websocket_server as websocket_server  # noqa: E402
from config.config_loader import load_config  # noqa: E402
from core.utils.cache.config import CacheType  # noqa: E402
from core.utils.cache.manager import cache_manager  # noqa: E402
from robot.runtime import RobotRuntime, set_runtime  # noqa: E402

#: Discovery has to finish inside a test, not inside a device's patience budget.
TEST_DISCOVERY_TIMEOUT = 10.0


def free_port() -> int:
    """An unused TCP port. Bound and released, so there is a small race by construction."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Backend:
    """A running server, and the pieces a test asserts against."""

    config: dict[str, Any]
    runtime: RobotRuntime
    ws_url: str
    port: int

    async def state(self, robot_id: str) -> Any:
        return await self.runtime.registry.get(robot_id)

    async def wait_for(
        self, predicate: Callable[[], Any], *, timeout: float = 10.0, interval: float = 0.05
    ) -> Any:
        """Poll an async predicate until it returns something truthy. Returns that value."""
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


@pytest.fixture
async def backend(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Backend]:
    """A nilo-server listening on an ephemeral port, with the robot subsystem attached."""
    # Two stubs, and both are load-bearing.
    #
    # The first is the server's shared providers. The second is the *per connection* ones:
    # `ConnectionHandler._initialize_components` builds a TTS, an ASR, a memory and an
    # intent provider for every session, and none of that is stubbed by the first. On a
    # machine where those packages are not installed it fails fast and the tests pass
    # anyway — which is why this went unnoticed on a laptop. On CI, where the full
    # requirements are installed, it succeeds: every connection loaded real providers, the
    # suite climbed about a gigabyte per test, and the runner was killed at 7 GB with no
    # message beyond "The operation was canceled".
    #
    # No robot test speaks audio or reaches a model, so the honest stub is nothing at all.
    monkeypatch.setattr(websocket_server, "initialize_modules", lambda *args, **kwargs: {})
    monkeypatch.setattr(connection.ConnectionHandler, "_initialize_components", lambda self: None)

    # Drop the cached config so each test gets its own dict, then let load_config put the
    # fresh one back: config/logger.py:setup_logging reads that cache entry from inside the
    # running loop and falls back to asyncio.run() when it is missing, which cannot work here.
    cache_manager.delete(CacheType.CONFIG, "main_config")
    config = await load_config()

    port = free_port()
    config["server"]["ip"] = "127.0.0.1"
    config["server"]["port"] = port
    config["server"]["auth_key"] = "integration-test-key"
    config["server"].setdefault("auth", {})["enabled"] = False

    runtime = RobotRuntime(discovery_timeout=TEST_DISCOVERY_TIMEOUT)
    set_runtime(runtime)

    server = websocket_server.WebSocketServer(config)
    task = asyncio.create_task(server.start(), name="integration-ws-server")
    await _wait_until_listening(port)
    try:
        yield Backend(config=config, runtime=runtime, ws_url=f"ws://127.0.0.1:{port}/nilo/v1/", port=port)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await runtime.aclose()
        set_runtime(None)
        cache_manager.delete(CacheType.CONFIG, "main_config")


async def _wait_until_listening(port: int, *, timeout: float = 10.0) -> None:
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
