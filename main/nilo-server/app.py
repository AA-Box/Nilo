# ruff: noqa: E402
"""nilo-server entry point: starts the WebSocket session server and the HTTP API."""
import asyncio
import signal
import sys
import uuid

from aioconsole import ainput
from config.logger import setup_logging
from config.opus_loader import setup_opus
from config.placeholders import is_placeholder
from config.settings import load_config

if not setup_opus():
    raise RuntimeError(
        "Failed to load the Opus library. Put the Opus shared library for your platform under libs/:\n"
        "  - Windows: libs/win/x64/opus.dll\n"
        "  - macOS (Apple Silicon): libs/mac/arm64/libopus.dylib\n"
        "  - macOS (Intel): libs/mac/x64/libopus.dylib\n"
        "  - Linux (ARM): libs/linux/arm64/libopus.so\n"
        "  - Linux (x64): libs/linux/x64/libopus.so"
    )

from core.http_server import SimpleHttpServer
from core.utils.gc_manager import get_gc_manager
from core.utils.util import check_ffmpeg_installed, get_local_ip, validate_mcp_endpoint
from core.websocket_server import WebSocketServer
from robot import __version__
from robot.api import API_HOST_ENV, API_PORT_ENV, DEFAULT_API_PORT, run_api, supervise
from robot.bootstrap import start_robot_subsystem
from robot.logging import install as install_robot_logging
from robot.protocol import registry_from_config
from robot.runtime import get_runtime

TAG = __name__
logger = setup_logging()


async def wait_for_exit() -> None:
    """Block until Ctrl-C / SIGTERM.

    Unix: add_signal_handler. Windows: rely on KeyboardInterrupt bubbling out of asyncio.run.
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        try:
            await asyncio.Future()
        except KeyboardInterrupt:
            pass


async def monitor_stdin():
    """Consume stdin so stray Enter presses do not block anything."""
    while True:
        await ainput()


def resolve_auth_key(config: dict) -> str:
    """server.auth_key > manager-api.secret > random. Used for OTA tokens, WebSocket and vision auth."""
    auth_key = config["server"].get("auth_key", "")
    if not auth_key or is_placeholder(auth_key):
        auth_key = config.get("manager-api", {}).get("secret", "")
        if not auth_key or is_placeholder(auth_key):
            auth_key = str(uuid.uuid4().hex)
    return auth_key


async def start_robot_api():
    """Start the robot management API on its own port. Returns its runner.

    Loopback and no token by default, which means it fails closed: every route but
    ``/health`` and ``/api/meta`` refuses until NILO_ROBOT_ADMIN_TOKEN is set, and control
    endpoints refuse off loopback until NILO_ROBOT_API_ALLOW_REMOTE_CONTROL is too
    (docs/robot-api.md).
    """
    import os

    runtime = get_runtime()

    async def memories(robot_id: str):
        return await runtime.memory(robot_id)

    return await run_api(
        runtime,
        memories,
        host=os.environ.get(API_HOST_ENV, "127.0.0.1"),
        port=int(os.environ.get(API_PORT_ENV, DEFAULT_API_PORT)),
    )


async def main():
    check_ffmpeg_installed()
    config = await load_config()
    config["server"]["auth_key"] = resolve_auth_key(config)

    # Robot code logs through stdlib logging; route it into the server's loguru sinks.
    install_robot_logging(config["log"].get("log_level", "INFO"))

    logger.bind(tag=TAG).info("nilo-server {} starting", __version__)

    # The robot subsystem's composition root. Before the WebSocket server, so the first
    # device to connect finds a configured runtime rather than a default one
    # (docs/configuration.md).
    await start_robot_subsystem()

    stdin_task = asyncio.create_task(monitor_stdin())

    # global GC manager (runs every 5 minutes)
    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    ws_server = WebSocketServer(config)
    ws_task = asyncio.create_task(ws_server.start())
    http_server = SimpleHttpServer(config)
    http_task = asyncio.create_task(http_server.start())
    # The robot management API. Supervised rather than fire-and-forget: this file creates
    # its tasks and never inspects them, so a port conflict would otherwise degrade the
    # server silently (docs/robot-architecture.md R5).
    robot_api_task = asyncio.create_task(start_robot_api())
    supervise(robot_api_task, lambda error: logger.bind(tag=TAG).error("robot management API failed: {}", error))

    read_config_from_api = config.get("read_config_from_api", False)
    http_port = int(config["server"].get("http_port", 8003))
    ws_port = int(config.get("server", {}).get("port", 8000))
    local_ip = get_local_ip()
    protocols = registry_from_config(config)

    for spec in protocols.enabled():
        if not read_config_from_api:
            logger.bind(tag=TAG).info(
                "OTA endpoint [{}]:\thttp://{}:{}{}", spec.name, local_ip, http_port, spec.ota_path
            )
        logger.bind(tag=TAG).info(
            "WebSocket endpoint [{}]:\tws://{}:{}{}", spec.name, local_ip, ws_port, spec.ws_path
        )
    logger.bind(tag=TAG).info("Vision endpoint:\thttp://{}:{}/mcp/vision/explain", local_ip, http_port)

    mcp_endpoint = config.get("mcp_endpoint", None)
    if mcp_endpoint is not None and not is_placeholder(mcp_endpoint):
        if validate_mcp_endpoint(mcp_endpoint):
            logger.bind(tag=TAG).info("MCP endpoint:\t{}", mcp_endpoint)
            # the configured value is the endpoint's /mcp/ URL; the server dials its /call/ twin
            config["mcp_endpoint"] = mcp_endpoint.replace("/mcp/", "/call/")
        else:
            logger.bind(tag=TAG).error("mcp_endpoint is not a valid MCP endpoint URL; ignoring it")
            config["mcp_endpoint"] = "<your-mcp-endpoint-websocket-url>"

    logger.bind(tag=TAG).info(
        "The WebSocket endpoints above are for devices; open the OTA endpoint in a browser to check the server."
    )

    try:
        await wait_for_exit()
    except asyncio.CancelledError:
        print("cancelled, cleaning up...")
    finally:
        await gc_manager.stop()

        # The robot subsystem first: closing it cancels discovery, stops the action pump
        # and its watchdog thread, closes every memory store and drains the event bus.
        # Without this the process exits with those still running and SQLite is closed by
        # the interpreter shutting down rather than by us (docs/robot-architecture.md R5).
        try:
            await asyncio.wait_for(get_runtime().aclose(), timeout=5.0)
        except Exception as error:  # a subsystem that will not close must not hang the exit
            logger.bind(tag=TAG).warning("closing the robot runtime failed: {}", error)

        if robot_api_task.done() and not robot_api_task.cancelled() and robot_api_task.exception() is None:
            await robot_api_task.result().cleanup()
        stdin_task.cancel()
        ws_task.cancel()
        http_task.cancel()
        robot_api_task.cancel()

        await asyncio.wait(
            [stdin_task, ws_task, http_task, robot_api_task],
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("nilo-server stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("interrupted.")
