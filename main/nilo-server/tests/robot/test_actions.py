"""The action model: the ten specs, the lifecycle, and the record they produce.

The lifecycle test is exhaustive rather than sampled. The state space is eight statuses by
eight requested statuses, which is 64 cases — small enough to enumerate completely, so
"no sequence of events reaches an illegal state" is proved here rather than sampled
(docs/robot-roadmap.md Phase 3). A random walk over the same table backs it up for
sequences, and a claim-leak check runs alongside it.

Nothing here touches a device, a queue or an executor.
"""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from pydantic import ValidationError

from robot.actions.model import (
    DEFAULT_TIMEOUT_S,
    SPEC_TYPES,
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    HeadAngleAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    RobotAction,
    StopAction,
    TurnAction,
    sort_key,
)
from robot.devices.mcp import sanitize_tool_name
from robot.state.actions import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    MOTION_ACTIONS,
    TERMINAL_STATUSES,
    ActionError,
    ActionPriority,
    ActionSource,
    ActionStatus,
    ActionType,
    IllegalTransition,
    RejectionReason,
    Resource,
    can_transition,
)
from robot.state.models import utcnow

ROBOT = "test-robot"


def action(spec=None, **kwargs) -> RobotAction:
    return RobotAction(spec or MoveAction(distance_mm=100), ROBOT, **kwargs)


# -- the vocabulary ---------------------------------------------------------------------------


def test_every_required_action_type_has_a_spec() -> None:
    """The ten actions the design names, and no silent gaps."""
    assert set(SPEC_TYPES) == set(ActionType)
    assert len(SPEC_TYPES) == 10


@pytest.mark.parametrize("action_type,spec_type", sorted(SPEC_TYPES.items(), key=lambda item: item[0].value))
def test_each_spec_declares_its_type_resources_and_tool(action_type, spec_type) -> None:
    assert spec_type.action_type is action_type
    assert spec_type.device_tool_name.startswith("robot.")
    assert spec_type.tool_name.startswith("robot_")
    assert spec_type.resources, f"{spec_type.__name__} claims no resource"
    assert set(spec_type.resources) <= set(Resource)


@pytest.mark.parametrize("spec_type", sorted(SPEC_TYPES.values(), key=lambda cls: cls.__name__))
def test_the_server_tool_name_is_the_sanitized_device_name(spec_type) -> None:
    """The two namespaces cannot drift: one is mechanically derived from the other.

    Discovery stores the sanitized name and calls back with the raw one
    (``robot/devices/mcp.py``), so a spec whose ``tool_name`` is not exactly
    ``sanitize_tool_name(device_tool_name)`` would look up a capability that does not
    exist and be rejected as unsupported — at runtime, on a robot.
    """
    assert spec_type.tool_name == sanitize_tool_name(spec_type.device_tool_name)


@pytest.mark.parametrize("spec_type", sorted(SPEC_TYPES.values(), key=lambda cls: cls.__name__))
def test_no_spec_parameter_is_a_float(spec_type) -> None:
    """The device MCP type system carries booleans, integers and strings only.

    A float parameter would be forwarded to firmware that cannot express it, and nothing
    in the server would reject it first (docs/mcp.md).
    """
    for name, field in spec_type.model_fields.items():
        assert field.annotation is not float, f"{spec_type.__name__}.{name} is a float"


@pytest.mark.parametrize("spec_type", sorted(SPEC_TYPES.values(), key=lambda cls: cls.__name__))
def test_no_spec_parameter_names_a_raw_actuator(spec_type) -> None:
    """The mechanical form of the design rule: the vocabulary is semantic, everywhere."""
    denylist = ("pwm", "duty", "servo_us", "wheel", "voltage", "motor", "current_ma", "torque")
    for name in spec_type.model_fields:
        assert not any(banned in name.lower() for banned in denylist), f"{spec_type.__name__}.{name}"


def test_motion_actions_are_exactly_the_ones_that_drive() -> None:
    driving = {kind for kind, spec in SPEC_TYPES.items() if Resource.DRIVE in spec.resources}
    assert driving == MOTION_ACTIONS | {ActionType.STOP}
    assert ActionType.STOP not in MOTION_ACTIONS  # stop is never gated as motion


def test_arguments_carry_their_units_in_the_name() -> None:
    assert MoveAction(distance_mm=300).arguments() == {"distance_mm": 300, "speed_mmps": 200}
    assert TurnAction(angle_deg=-90, speed_dps=45).arguments() == {"angle_deg": -90, "speed_dps": 45}
    assert LookAtAction(x_pct=10, y_pct=90).arguments() == {"x_pct": 10, "y_pct": 90}
    assert StopAction().arguments() == {}


def test_a_capture_without_a_question_sends_no_question_key() -> None:
    assert CaptureImageAction().arguments() == {}
    assert CaptureImageAction(question="who is there?").arguments() == {"question": "who is there?"}


@pytest.mark.parametrize(
    "spec,expected",
    [
        (MoveAction(distance_mm=1000, speed_mmps=200), 5.0),
        (TurnAction(angle_deg=180, speed_dps=90), 2.0),
        (AnimationAction(name="greet", duration_ms=1500), 1.5),
        (FollowTargetAction(target_id="person-1", duration_ms=4000), 4.0),
        (StopAction(), 0.0),
    ],
)
def test_a_spec_that_can_estimate_its_duration_does(spec, expected) -> None:
    assert spec.estimated_duration_s() == pytest.approx(expected)


def test_a_spec_that_cannot_estimate_falls_back_to_the_default_budget() -> None:
    assert LiftAction(height_pct=50).estimated_duration_s() is None
    assert LiftAction(height_pct=50).suggested_timeout_s() == DEFAULT_TIMEOUT_S


def test_specs_are_frozen_and_reject_unknown_parameters() -> None:
    spec = MoveAction(distance_mm=100)
    with pytest.raises(ValidationError):
        spec.distance_mm = 200
    with pytest.raises(ValidationError):
        MoveAction(distance_mm=100, pwm_duty=90)


def test_specs_do_not_range_check_their_own_numbers() -> None:
    """Bounds are configurable policy. A spec that raised here would turn a rejection the
    caller can read into an exception at the call site (docs/safety-model.md)."""
    assert MoveAction(distance_mm=999_999).distance_mm == 999_999
    assert TurnAction(angle_deg=100_000).angle_deg == 100_000
    assert LiftAction(height_pct=-5).height_pct == -5


@pytest.mark.parametrize(
    "spec_type,kwargs",
    [(AnimationAction, {"name": "  "}), (ExpressionAction, {"emotion": ""}), (FollowTargetAction, {"target_id": ""})],
)
def test_a_blank_name_is_a_programming_error_not_a_policy_one(spec_type, kwargs) -> None:
    with pytest.raises(ValidationError):
        spec_type(**kwargs)


# -- the lifecycle ------------------------------------------------------------------------------


def test_the_transition_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(ActionStatus)
    assert TERMINAL_STATUSES | ACTIVE_STATUSES == set(ActionStatus)
    assert not TERMINAL_STATUSES & ACTIVE_STATUSES


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
def test_no_transition_leaves_a_terminal_status(status) -> None:
    assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_the_happy_path_is_pending_starting_running_succeeded() -> None:
    item = action()
    assert item.status is ActionStatus.PENDING
    assert item.started_at is None
    item.transition(ActionStatus.STARTING)
    assert item.started_at is not None
    item.transition(ActionStatus.RUNNING)
    record = item.transition(ActionStatus.SUCCEEDED, result={"completed": True})
    assert record.status is ActionStatus.SUCCEEDED
    assert record.finished_at is not None
    assert record.duration_s is not None and record.duration_s >= 0
    assert record.result == {"completed": True}


@pytest.mark.parametrize("current", sorted(ActionStatus, key=lambda s: s.value))
@pytest.mark.parametrize("requested", sorted(ActionStatus, key=lambda s: s.value))
def test_every_transition_pair_either_is_legal_or_raises(current, requested) -> None:
    """All 64 pairs, exhaustively. An illegal transition raises; it is never absorbed."""
    item = action()
    _force(item, current)
    if can_transition(current, requested):
        assert item.transition(requested).status is requested
        return
    with pytest.raises(IllegalTransition) as raised:
        item.transition(requested)
    assert raised.value.current is current
    assert raised.value.requested is requested
    assert item.status is current  # and nothing changed


def test_a_random_walk_never_reaches_an_illegal_state_or_leaks_a_claim() -> None:
    """Sequences, not single steps: 2000 random walks over the table from PENDING.

    The invariant checked after every step is the one the executor depends on: an action
    holds its resources while it is active and holds nothing once it is terminal, so no
    sequence of events can leave a claim behind.
    """
    rng = random.Random(20260913)
    statuses = sorted(ActionStatus, key=lambda s: s.value)
    for _ in range(2000):
        item = action()
        for _ in range(8):
            requested = rng.choice(statuses)
            legal = can_transition(item.status, requested)
            try:
                item.transition(requested)
            except IllegalTransition:
                assert not legal
            else:
                assert legal
            assert item.status.is_terminal != item.status.is_active
            assert item.is_terminal == (item.finished_at is not None)
            if item.is_terminal:
                break
        # A terminal action is done; an active one has not set its completion event.
        assert item.is_terminal == item._done.is_set()


def test_a_terminal_action_refuses_a_second_completion() -> None:
    """The duplicate-completion guard, at the model level.

    The executor checks ``is_terminal`` before settling, so a repeated device
    notification is dropped. If it ever stopped checking, this is what would happen.
    """
    item = action()
    item.transition(ActionStatus.STARTING)
    item.transition(ActionStatus.RUNNING)
    item.transition(ActionStatus.SUCCEEDED)
    with pytest.raises(IllegalTransition):
        item.transition(ActionStatus.SUCCEEDED)
    with pytest.raises(IllegalTransition):
        item.transition(ActionStatus.FAILED)


def test_started_at_is_set_once_and_never_moved() -> None:
    item = action()
    item.transition(ActionStatus.STARTING)
    first = item.started_at
    item.transition(ActionStatus.RUNNING)
    assert item.started_at == first


def test_a_rejection_carries_a_typed_reason_and_is_terminal_immediately() -> None:
    item = action()
    record = item.reject(RejectionReason.CLIFF_HAZARD, "a cliff sensor is asserted")
    assert record.status is ActionStatus.REJECTED
    assert record.rejection is RejectionReason.CLIFF_HAZARD
    assert record.error is not None and "cliff" in record.error.message
    assert record.started_at is None  # it never ran
    assert item.is_terminal


async def test_wait_returns_the_record_as_soon_as_the_action_is_terminal() -> None:
    item = action()
    item.transition(ActionStatus.STARTING)
    item.transition(ActionStatus.RUNNING)
    item.transition(ActionStatus.FAILED, error=ActionError(code="obstacle", message="wall"))
    record = await item.wait(timeout=0.1)
    assert record.status is ActionStatus.FAILED
    assert record.error is not None and record.error.code == "obstacle"


# -- the record ---------------------------------------------------------------------------------


def test_the_record_carries_every_field_the_design_requires() -> None:
    item = action(source=ActionSource.LLM, priority=ActionPriority.HIGH, timeout_s=4.0, ttl_s=9.0)
    record = item.record()
    for field in (
        "action_id", "robot_id", "created_at", "started_at", "finished_at",
        "timeout_s", "priority", "source", "status", "parameters",
    ):
        assert field in record.model_fields, field
    assert record.source is ActionSource.LLM
    assert record.priority is ActionPriority.HIGH
    assert record.timeout_s == 4.0
    assert record.ttl_s == 9.0
    assert record.parameters == {"distance_mm": 100, "speed_mmps": 200}
    assert record.resources == (Resource.DRIVE,)
    assert record.result is None and record.error is None


def test_the_record_keeps_a_field_that_never_reaches_the_wire() -> None:
    """``parameters`` is the whole request, not the narrower wire arguments.

    A stop sends no arguments at all, and its ``reason`` is the most useful thing about it
    when someone is reading the log afterwards.
    """
    stop = RobotAction(StopAction(reason="a person stepped in front"), ROBOT)
    assert stop.spec.arguments() == {}
    assert stop.record().parameters == {"reason": "a person stepped in front"}


def test_a_record_is_a_snapshot_and_not_a_live_view() -> None:
    """Everything published on the bus is a record, so a subscriber cannot mutate an action."""
    item = action()
    before = item.record()
    item.transition(ActionStatus.STARTING)
    assert before.status is ActionStatus.PENDING
    assert item.record().status is ActionStatus.STARTING
    with pytest.raises(ValidationError):
        before.status = ActionStatus.RUNNING


def test_age_is_measured_from_creation_which_is_what_the_ttl_checks() -> None:
    created = utcnow() - timedelta(seconds=30)
    item = action(created_at=created)
    assert item.record().age_s() == pytest.approx(30.0, abs=1.0)


def test_the_default_timeout_comes_from_the_spec_estimate() -> None:
    item = action(MoveAction(distance_mm=1000, speed_mmps=200))
    assert item.timeout_s == pytest.approx(8.0)  # 5 s of travel plus the margin
    assert action(MoveAction(distance_mm=1000), timeout_s=2.5).timeout_s == 2.5


# -- ordering and conflicts ------------------------------------------------------------------------


def test_dispatch_order_is_priority_first_then_oldest() -> None:
    base = utcnow()
    low_old = action(priority=ActionPriority.LOW, created_at=base)
    normal_new = action(priority=ActionPriority.NORMAL, created_at=base + timedelta(seconds=5))
    normal_old = action(priority=ActionPriority.NORMAL, created_at=base + timedelta(seconds=1))
    high = action(priority=ActionPriority.HIGH, created_at=base + timedelta(seconds=9))
    ordered = sorted([low_old, normal_new, normal_old, high], key=sort_key)
    assert ordered == [high, normal_old, normal_new, low_old]


def test_two_actions_conflict_when_they_share_a_subsystem_on_one_robot() -> None:
    assert action(MoveAction(distance_mm=100)).conflicts_with(action(TurnAction(angle_deg=90)))
    assert not action(MoveAction(distance_mm=100)).conflicts_with(action(LookAtAction()))
    assert action(LookAtAction()).conflicts_with(action(HeadAngleAction(pitch_deg=10)))
    assert action(ExpressionAction(emotion="happy")).conflicts_with(action(AnimationAction(name="greet")))
    # A follow claims three subsystems, so it collides with drive, head and camera work.
    follow = action(FollowTargetAction(target_id="person-1"))
    assert follow.conflicts_with(action(MoveAction(distance_mm=100)))
    assert follow.conflicts_with(action(LookAtAction()))
    assert follow.conflicts_with(action(CaptureImageAction()))


def test_actions_on_different_robots_never_conflict() -> None:
    mine = RobotAction(MoveAction(distance_mm=100), "robot-a")
    yours = RobotAction(MoveAction(distance_mm=100), "robot-b")
    assert not mine.conflicts_with(yours)


def _force(item: RobotAction, status: ActionStatus) -> None:
    """Put an action into ``status`` by the shortest legal route, for a table-driven test."""
    routes: dict[ActionStatus, tuple[ActionStatus, ...]] = {
        ActionStatus.PENDING: (),
        ActionStatus.STARTING: (ActionStatus.STARTING,),
        ActionStatus.RUNNING: (ActionStatus.STARTING, ActionStatus.RUNNING),
        ActionStatus.SUCCEEDED: (ActionStatus.STARTING, ActionStatus.SUCCEEDED),
        ActionStatus.FAILED: (ActionStatus.STARTING, ActionStatus.FAILED),
        ActionStatus.CANCELLED: (ActionStatus.CANCELLED,),
        ActionStatus.TIMED_OUT: (ActionStatus.TIMED_OUT,),
        ActionStatus.REJECTED: (ActionStatus.REJECTED,),
    }
    for step in routes[status]:
        item.transition(step)
    assert item.status is status
