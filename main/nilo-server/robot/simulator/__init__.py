"""A complete simulated robot that speaks the Nilo device protocol.

    python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --robot-id nilo-sim-01

It is a *client*, not a test double wired into the server: it opens the real WebSocket
route, completes the real handshake, publishes its hardware as device MCP tools and reports
telemetry as JSON-RPC notifications. Everything the backend learns about it, it learns over
the socket — which is what makes it usable as the thing every robot test runs against
(docs/robot-simulator.md).

Nothing in this package imports ``core/``; ``websockets`` is imported lazily inside the
session, so the world model, the physics step and the scenario runner are all importable
and testable with the dev-only dependency slice.
"""

from robot.simulator.camera import CameraFailure, encode_png, render_frame
from robot.simulator.clock import Clock, ManualClock, RealClock, build_clock
from robot.simulator.device import DEFAULT_SERVER_URL, SimulatedRobot, SimulatorConfig, fetch_status
from robot.simulator.scenarios import (
    BUILTIN_SCENARIOS,
    STEP_ACTIONS,
    Faults,
    Scenario,
    ScenarioRunner,
    Step,
    get_scenario,
    load_scenario_file,
)
from robot.simulator.state import MotionKind, MotionOutcome, MotionResult, RobotProfile, RobotSimState
from robot.simulator.tools import EMOTIONS, TOOL_NAMES, TOOL_SPECS, TOOLS_BY_NAME, ToolSpec
from robot.simulator.world import Cliff, Obstacle, Person, World, WorldObject, WorldSpec, default_room

__all__ = [
    "BUILTIN_SCENARIOS",
    "DEFAULT_SERVER_URL",
    "EMOTIONS",
    "STEP_ACTIONS",
    "TOOLS_BY_NAME",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "CameraFailure",
    "Cliff",
    "Clock",
    "Faults",
    "ManualClock",
    "MotionKind",
    "MotionOutcome",
    "MotionResult",
    "Obstacle",
    "Person",
    "RealClock",
    "RobotProfile",
    "RobotSimState",
    "Scenario",
    "ScenarioRunner",
    "SimulatedRobot",
    "SimulatorConfig",
    "Step",
    "ToolSpec",
    "World",
    "WorldObject",
    "WorldSpec",
    "build_clock",
    "default_room",
    "encode_png",
    "fetch_status",
    "get_scenario",
    "load_scenario_file",
    "render_frame",
]
