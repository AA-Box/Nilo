"""The tool surface: what it advertises, what it refuses, and what it can never say.

Three groups:

* **The vocabulary is semantic.** No schema names an actuator, no schema carries a float,
  and every tool the design note lists is present. These are the mechanical form of the
  rule that the LLM never gets a tool that sets a motor value.
* **Arguments are validated before anything moves.** Unknown keys, wrong types, values
  past the configured ceiling.
* **Permissions.** Four classes, a configurable policy, and the one tool the policy may
  never refuse.
"""
from __future__ import annotations

import pytest

from robot.agent.permissions import ToolPermission, ToolPolicy, TurnOrigin
from robot.agent.tools import TOOL_NAMES, TOOL_SPECS, RobotToolkit, ToolError
from robot.behavior.base import AutonomyMode
from robot.runtime import RobotRuntime
from robot.safety.limits import SafetyLimits
from tests.robot.conftest import ROBOT_ID, attach_fake_robot, until

#: Words that must never appear in a tool schema. If one does, the model has been handed
#: a raw actuator and every layer below it has become decorative
#: (docs/robot-architecture.md Sect. 3).
ACTUATOR_WORDS = (
    "pwm",
    "duty",
    "servo_us",
    "wheel_speed",
    "wheel_rpm",
    "voltage",
    "motor",
    "torque",
    "encoder",
    "register",
    "gpio",
)

#: The tools the design note requires, in its own dotted spelling.
REQUIRED_TOOLS = (
    "robot.move",
    "robot.turn",
    "robot.stop",
    "robot.look_at",
    "robot.follow_person",
    "robot.stop_following",
    "robot.play_animation",
    "robot.set_expression",
    "robot.capture_image",
    "robot.inspect_object",
    "robot.get_battery",
    "robot.get_state",
    "robot.remember",
    "robot.recall",
)


@pytest.fixture
def toolkit(agent_runtime):
    runtime, _ = agent_runtime
    return RobotToolkit(runtime, ROBOT_ID)


@pytest.fixture
async def bounded_runtime():
    """A runtime whose safety limits are half the defaults, so a ceiling is easy to cross."""
    runtime = RobotRuntime(discovery_timeout=1.0, limits=SafetyLimits(max_distance_mm=500))
    channel = await attach_fake_robot(runtime)
    try:
        yield runtime, channel
    finally:
        await runtime.aclose()


# -- the vocabulary -------------------------------------------------------------------------------


def test_every_required_tool_is_offered():
    assert {spec.qualified_name for spec in TOOL_SPECS} == set(REQUIRED_TOOLS)
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)) == len(REQUIRED_TOOLS)


def test_no_tool_schema_names_an_actuator(toolkit):
    """The mechanical form of the design rule."""
    for description in toolkit.function_descriptions():
        blob = repr(description).lower()
        offending = [word for word in ACTUATOR_WORDS if word in blob]
        assert not offending, f"{description['function']['name']} mentions {offending}"


def test_no_tool_schema_contains_a_float(toolkit):
    """Nothing on the server rejects a float and the firmware vocabulary cannot carry one,
    so a ``number`` parameter is a latent bug rather than a style question."""
    for description in toolkit.function_descriptions():
        for name, schema in description["function"]["parameters"]["properties"].items():
            assert schema["type"] in {"integer", "string", "boolean"}, (
                f"{description['function']['name']}.{name} is {schema['type']}"
            )


def test_wire_names_are_flat_and_the_qualified_names_are_dotted():
    """The tool namespace is flat and a function name may not contain a dot, so every tool
    carries both spellings and they have to stay in step."""
    for spec in TOOL_SPECS:
        assert spec.name == spec.qualified_name.replace(".", "_")
        assert "." not in spec.name


async def test_schemas_carry_the_live_safety_limits(bounded_runtime):
    runtime, _ = bounded_runtime
    kit = RobotToolkit(runtime, ROBOT_ID)
    move = next(d for d in kit.function_descriptions() if d["function"]["name"] == "robot_move")
    distance = move["function"]["parameters"]["properties"]["distance_mm"]
    assert distance["maximum"] == 500
    assert distance["minimum"] == -500


def test_the_expression_schema_enumerates_the_closed_set(toolkit):
    expression = next(
        d for d in toolkit.function_descriptions() if d["function"]["name"] == "robot_set_expression"
    )
    choices = expression["function"]["parameters"]["properties"]["emotion"]["enum"]
    assert "happy" in choices and "curious" in choices


# -- argument validation ----------------------------------------------------------------------------


def test_unknown_arguments_are_rejected(toolkit):
    with pytest.raises(ToolError, match="speed"):
        toolkit.validate("robot_move", {"distance_mm": 100, "speed": 200})


def test_a_missing_required_argument_is_rejected(toolkit):
    with pytest.raises(ToolError, match="distance_mm"):
        toolkit.validate("robot_move", {})


def test_a_non_integer_distance_is_rejected(toolkit):
    with pytest.raises(ToolError, match="distance_mm"):
        toolkit.validate("robot_move", {"distance_mm": "a long way"})


def test_a_float_distance_is_rejected(toolkit):
    """0.25 metres is 250 millimetres. The vocabulary has no float, so say so."""
    with pytest.raises(ToolError):
        toolkit.validate("robot_move", {"distance_mm": 250.5})


async def test_a_distance_past_the_configured_ceiling_is_rejected(bounded_runtime):
    runtime, _ = bounded_runtime
    kit = RobotToolkit(runtime, ROBOT_ID)
    with pytest.raises(ToolError, match="500"):
        kit.validate("robot_move", {"distance_mm": 900})
    assert kit.validate("robot_move", {"distance_mm": -400}).distance_mm == -400


def test_an_unknown_expression_is_rejected(toolkit):
    with pytest.raises(ToolError, match="unknown expression"):
        toolkit.validate("robot_set_expression", {"emotion": "smug"})


def test_an_out_of_range_percentage_is_rejected(toolkit):
    with pytest.raises(ToolError, match="x_pct"):
        toolkit.validate("robot_look_at", {"x_pct": 140})


def test_an_unknown_tool_is_an_outcome_not_an_exception(toolkit):
    with pytest.raises(ToolError, match="no robot tool"):
        toolkit.validate("robot_launch_missile", {})


async def test_calling_an_unknown_tool_returns_a_failure(toolkit):
    outcome = await toolkit.call("robot_launch_missile", {})
    assert not outcome.ok
    assert "no robot tool" in outcome.error


async def test_a_validation_failure_never_reaches_the_device(toolkit, agent_runtime):
    _, channel = agent_runtime
    outcome = await toolkit.call("robot_move", {"distance_mm": 999_999})
    assert not outcome.ok
    assert outcome.refused_by == "argument_validation"
    assert channel.called("robot.motion.move") == ()


# -- permissions ------------------------------------------------------------------------------------


def test_the_four_permission_classes_cover_every_tool():
    used = {spec.permission for spec in TOOL_SPECS}
    assert used == set(ToolPermission)


@pytest.mark.parametrize(
    ("tool", "permission"),
    [
        ("robot_move", ToolPermission.MOTION),
        ("robot_set_expression", ToolPermission.EXPRESSIVE),
        ("robot_get_battery", ToolPermission.READ_ONLY),
        ("robot_capture_image", ToolPermission.PRIVILEGED),
    ],
)
def test_tools_are_classified_as_the_design_note_says(tool: str, permission: ToolPermission):
    assert next(spec for spec in TOOL_SPECS if spec.name == tool).permission is permission


def test_an_autonomous_turn_gets_read_only_and_expressive_tools_only(toolkit):
    available = toolkit.available(TurnOrigin.BEHAVIOR)
    assert "robot_get_state" in available
    assert "robot_set_expression" in available
    assert "robot_move" not in available
    assert "robot_capture_image" not in available


def test_a_user_turn_gets_everything(toolkit):
    assert set(toolkit.available(TurnOrigin.USER)) == set(TOOL_NAMES)


def test_stop_is_available_even_on_an_autonomous_turn_with_autonomy_off(toolkit):
    """A robot that will not stop because its autonomy mode is off is the wrong failure."""
    available = toolkit.available(TurnOrigin.BEHAVIOR, AutonomyMode.OFF)
    assert available == ("robot_stop",)


def test_motion_is_refused_below_the_configured_autonomy_mode(agent_runtime):
    runtime, _ = agent_runtime
    kit = RobotToolkit(runtime, ROBOT_ID, policy=ToolPolicy(motion_min_autonomy=AutonomyMode.FULL))
    available = kit.available(TurnOrigin.USER, AutonomyMode.NORMAL)
    assert "robot_move" not in available
    assert "robot_stop" in available
    assert "robot_get_state" in available


async def test_a_refused_tool_never_reaches_the_device(toolkit, agent_runtime):
    _, channel = agent_runtime
    outcome = await toolkit.call("robot_move", {"distance_mm": 100}, origin=TurnOrigin.BEHAVIOR)
    assert not outcome.ok
    assert outcome.refused_by == "permission_policy"
    assert channel.called("robot.motion.move") == ()


async def test_a_policy_can_be_configured_to_allow_autonomous_motion(agent_runtime):
    runtime, channel = agent_runtime
    kit = RobotToolkit(
        runtime,
        ROBOT_ID,
        policy=ToolPolicy(
            autonomous=frozenset({ToolPermission.READ_ONLY, ToolPermission.MOTION}),
        ),
    )
    outcome = await kit.call("robot_move", {"distance_mm": 100}, origin=TurnOrigin.BEHAVIOR)
    assert outcome.ok, outcome.error
    assert await until(lambda: channel.called("robot.motion.move"))
