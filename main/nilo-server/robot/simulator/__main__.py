"""Command line entry point for the robot simulator.

    python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ \
        --robot-id nilo-sim-01 --scenario person_enters_room

Logging goes through stdlib ``logging`` configured here, never through
``config/logger.py:setup_logging`` — the simulator is a separate process from the server
and must run without the server's config file (docs/robot-architecture.md R12).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Sequence
from typing import Any

from robot.simulator.device import DEFAULT_SERVER_URL, SimulatedRobot, SimulatorConfig, fetch_status
from robot.simulator.scenarios import BUILTIN_SCENARIOS, Scenario, get_scenario, load_scenario_file
from robot.simulator.state import DEFAULT_MAX_SPEED_MMPS, DEFAULT_MAX_TURN_DPS, RobotProfile
from robot.simulator.world import Cliff, Obstacle, Person, World, WorldSpec, default_room

logger = logging.getLogger("robot.simulator")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m robot.simulator",
        description="A simulated Nilo robot: connects to a running server, publishes its hardware "
        "as device MCP tools, reports telemetry and plays a scenario.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    connection = parser.add_argument_group("connection")
    connection.add_argument("--server", default=DEFAULT_SERVER_URL, help="WebSocket URL of the nilo route")
    connection.add_argument("--robot-id", default="nilo-sim-01", help="device id sent in the handshake header")
    connection.add_argument("--client-id", default="nilo-simulator", help="client id sent in the handshake header")
    connection.add_argument("--token", default=None, help="bearer token, when server.auth.enabled is true")
    connection.add_argument("--no-reconnect", action="store_true", help="exit instead of redialling a dropped link")
    connection.add_argument(
        "--reconnect-delay", type=float, default=1.0, metavar="SEC", help="wait before redialling"
    )

    run = parser.add_argument_group("run")
    run.add_argument("--scenario", default="idle", help=f"built-in scenario: {', '.join(sorted(BUILTIN_SCENARIOS))}")
    run.add_argument("--scenario-file", default=None, metavar="PATH", help="YAML or JSON scenario file")
    run.add_argument("--list-scenarios", action="store_true", help="print the built-in scenarios and exit")
    run.add_argument("--print-scenario", action="store_true", help="print the resolved scenario as JSON and exit")
    run.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second")
    run.add_argument(
        "--duration", type=float, default=0.0, metavar="SEC",
        help="stop after this much simulated time; 0 runs forever",
    )
    run.add_argument("--tick-ms", type=int, default=100, help="physics step, in simulated milliseconds")
    run.add_argument("--telemetry-ms", type=int, default=1000, help="notifications/telemetry period")
    run.add_argument("--seed", type=int, default=0, help="seed for simulated sensor noise")
    run.add_argument("--tools-page-size", type=int, default=0, help="tools per tools/list page; 0 sends one page")
    run.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")

    hardware = parser.add_argument_group("hardware")
    hardware.add_argument("--max-speed-mmps", type=int, default=DEFAULT_MAX_SPEED_MMPS)
    hardware.add_argument("--max-turn-dps", type=int, default=DEFAULT_MAX_TURN_DPS)
    hardware.add_argument(
        "--sensor-noise-mm", type=float, default=0.0, help="Gaussian noise on distance readings; 0 keeps runs exact"
    )
    hardware.add_argument("--battery", type=int, default=None, metavar="PCT", help="starting battery charge")
    hardware.add_argument(
        "--camera-fixtures", default=None, metavar="DIR", help="serve images from here instead of rendering"
    )

    world = parser.add_argument_group("world")
    world.add_argument("--room", default=None, metavar="W_MM,D_MM", help="room size, default 4000,3000")
    world.add_argument(
        "--obstacle", action="append", default=[], metavar="X_MM,Y_MM,R_MM", help="add an obstacle (repeatable)"
    )
    world.add_argument(
        "--cliff", action="append", default=[], metavar="X0,Y0,X1,Y1", help="add a cliff region in mm (repeatable)"
    )
    world.add_argument("--person", action="append", default=[], metavar="X_MM,Y_MM", help="add a person (repeatable)")
    world.add_argument("--no-dock", action="store_true", help="remove the charging dock")

    faults = parser.add_argument_group("failure injection")
    faults.add_argument("--packet-delay-ms", type=int, default=0, help="delay every frame the simulator sends")
    faults.add_argument(
        "--tool-timeout", action="append", default=[], metavar="TOOL", help="accept this tool's call and never answer"
    )
    faults.add_argument(
        "--tool-error", action="append", default=[], metavar="TOOL", help="answer this tool with an error"
    )
    faults.add_argument("--motor-failure", action="store_true", help="fail every motion in flight")
    faults.add_argument("--camera-failure", action="store_true", help="fail robot.camera.capture")
    faults.add_argument("--drop-notifications", action="store_true", help="stop sending telemetry, keep the session up")
    faults.add_argument(
        "--disconnect-at", type=float, default=None, metavar="SEC",
        help="abort the TCP connection at this simulated time",
    )

    status = parser.add_argument_group("state inspection")
    status.add_argument(
        "--status-port", type=int, default=8090, help="port for the read-only status endpoint; 0 disables"
    )
    status.add_argument("--status-host", default="127.0.0.1", help="bind address for the status endpoint")
    status.add_argument("--status", action="store_true", help="print a running simulator's state and exit")
    return parser


def _numbers(raw: str, count: int, flag: str) -> list[int]:
    parts = [piece.strip() for piece in raw.split(",")]
    if len(parts) != count:
        raise SystemExit(f"{flag}: expected {count} comma-separated integers, got {raw!r}")
    try:
        return [int(piece) for piece in parts]
    except ValueError:
        raise SystemExit(f"{flag}: expected integers, got {raw!r}") from None


def resolve_scenario(args: argparse.Namespace) -> Scenario:
    """The scenario a run uses, with the CLI's faults and world overrides folded in."""
    scenario = load_scenario_file(args.scenario_file) if args.scenario_file else get_scenario(args.scenario)
    faults = scenario.faults
    faults.packet_delay_ms = args.packet_delay_ms or faults.packet_delay_ms
    faults.tool_timeout = list({*faults.tool_timeout, *args.tool_timeout})
    faults.tool_error = list({*faults.tool_error, *args.tool_error})
    faults.motor_failure = faults.motor_failure or args.motor_failure
    faults.camera_failure = faults.camera_failure or args.camera_failure
    faults.drop_notifications = faults.drop_notifications or args.drop_notifications
    if args.disconnect_at is not None:
        faults.disconnect_at_s = args.disconnect_at
    faults.reconnect = not args.no_reconnect
    if args.battery is not None:
        scenario.start_battery_pct = max(0, min(100, args.battery))
    if args.duration:
        scenario.duration_s = args.duration
    scenario.world = _resolve_world(args, scenario.world)
    return scenario


def _resolve_world(args: argparse.Namespace, spec: WorldSpec | None) -> WorldSpec:
    if spec is None:
        if args.room:
            width_mm, depth_mm = _numbers(args.room, 2, "--room")
            spec = default_room(width_mm / 1000.0, depth_mm / 1000.0)
        else:
            spec = default_room()
    for index, raw in enumerate(args.obstacle, start=1):
        x_mm, y_mm, r_mm = _numbers(raw, 3, "--obstacle")
        spec.obstacles.append(
            Obstacle(id=f"cli-obstacle-{index}", x_m=x_mm / 1000.0, y_m=y_mm / 1000.0, radius_m=r_mm / 1000.0)
        )
    for index, raw in enumerate(args.cliff, start=1):
        x0, y0, x1, y1 = _numbers(raw, 4, "--cliff")
        spec.cliffs.append(
            Cliff(id=f"cli-cliff-{index}", x0_m=x0 / 1000.0, y0_m=y0 / 1000.0, x1_m=x1 / 1000.0, y1_m=y1 / 1000.0)
        )
    for index, raw in enumerate(args.person, start=1):
        x_mm, y_mm = _numbers(raw, 2, "--person")
        spec.people.append(Person(id=f"cli-person-{index}", x_m=x_mm / 1000.0, y_m=y_mm / 1000.0))
    if args.no_dock:
        spec.dock_x_m = None
        spec.dock_y_m = None
    return spec


def build_robot(args: argparse.Namespace) -> SimulatedRobot:
    from robot.simulator.clock import RealClock

    scenario = resolve_scenario(args)
    config = SimulatorConfig(
        server_url=args.server,
        robot_id=args.robot_id,
        client_id=args.client_id,
        token=args.token,
        tick_ms=max(1, args.tick_ms),
        telemetry_ms=max(1, args.telemetry_ms),
        tools_page_size=max(0, args.tools_page_size),
        duration_s=scenario.duration_s if args.duration else 0.0,
        reconnect=not args.no_reconnect,
        reconnect_delay_s=args.reconnect_delay,
        status_port=args.status_port,
        status_host=args.status_host,
        camera_fixtures=args.camera_fixtures,
        seed=args.seed,
        profile=RobotProfile(
            max_speed_mmps=args.max_speed_mmps,
            max_turn_dps=args.max_turn_dps,
            sensor_noise_mm=args.sensor_noise_mm,
        ),
    )
    return SimulatedRobot(config, scenario=scenario, clock=RealClock(args.speed), world=World(scenario.world))


async def _run(robot: SimulatedRobot) -> None:
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, robot.stop)
    await robot.run()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.list_scenarios:
        for name, scenario in sorted(BUILTIN_SCENARIOS.items()):
            print(f"{name:<22} {scenario.duration_s:>5.0f}s  {scenario.description}")
        return 0

    if args.status:
        try:
            print(json.dumps(fetch_status(args.status_host, args.status_port or 8090), indent=2))
        except OSError as exc:
            print(f"no simulator answering on {args.status_host}:{args.status_port or 8090}: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        robot = build_robot(args)
    except (KeyError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_scenario:
        payload: dict[str, Any] = robot.scenario.model_dump(mode="json")
        print(json.dumps(payload, indent=2))
        return 0

    logger.info(
        "simulator %s -> %s, scenario %s, clock x%.1f",
        robot.config.robot_id,
        robot.config.server_url,
        robot.scenario.name,
        robot.clock.speed,
    )
    try:
        asyncio.run(_run(robot))
    except KeyboardInterrupt:
        print("interrupted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
