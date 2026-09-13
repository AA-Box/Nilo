"""The safety policy: limits, sensor gating, TTL, rate limits, and the emergency stop.

Two themes run through the whole file.

**The answer does not depend on who asked.** Almost every rejection test is parameterized
over all five :class:`~robot.state.actions.ActionSource` values, because "the LLM cannot
override a safety rejection" is only true if it is true for every check, not just the ones
someone remembered to gate. The single asymmetry — ``SAFETY`` may command a stop while the
latch is engaged — is tested explicitly rather than assumed.

**A limit breach is a rejection, not a clamp.** ``policy.evaluate`` never returns an
adjusted request. A move beyond the ceiling comes back ``REJECTED`` with the number that
failed, so the bug that produced it is visible (docs/safety.md).

Nothing here needs an event loop: the policy is a pure function.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from robot.actions.model import (
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    HeadAngleAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    StopAction,
    TurnAction,
)
from robot.safety import EmergencyStop, RobotSafetyPolicy, SafetyContext, SafetyLimits
from robot.safety.limits import limits_from_mapping, load_limits
from robot.state.actions import ActionSource, RejectionReason
from robot.state.models import RobotBatteryState, RobotSensorState, utcnow
from tests.robot.conftest import ROBOT_ID, robot_state

ALL_SOURCES = sorted(ActionSource, key=lambda source: source.value)
#: The four sources that have no special standing whatsoever.
ORDINARY_SOURCES = [source for source in ALL_SOURCES if source is not ActionSource.SAFETY]


@pytest.fixture
def policy() -> RobotSafetyPolicy:
    return RobotSafetyPolicy(SafetyLimits(), EmergencyStop())


#: Distinguishes "use a healthy default robot" from "there is no such robot".
_DEFAULT = object()


def context(state=_DEFAULT, **kwargs) -> SafetyContext:
    return SafetyContext(
        robot_id=ROBOT_ID,
        state=robot_state() if state is _DEFAULT else state,
        now=kwargs.pop("now", utcnow()),
        **kwargs,
    )


def sensors(**kwargs) -> RobotSensorState:
    readings = kwargs.pop("readings", {"front_mm": 1500.0})
    return RobotSensorState(readings=readings, **kwargs)


# -- the happy path ------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_reasonable_request_is_allowed_from_every_source(policy, source) -> None:
    assert policy.evaluate(MoveAction(distance_mm=300), source, context())


@pytest.mark.parametrize(
    "spec",
    [
        MoveAction(distance_mm=-300),
        TurnAction(angle_deg=90),
        LookAtAction(x_pct=20, y_pct=80),
        HeadAngleAction(pitch_deg=10, yaw_deg=-30),
        LiftAction(height_pct=40),
        ExpressionAction(emotion="happy"),
        AnimationAction(name="greet"),
        CaptureImageAction(question="who is that?"),
        FollowTargetAction(target_id="person-1"),
        StopAction(),
    ],
    ids=lambda spec: spec.action_type.value,
)
def test_every_action_type_is_dispatchable_on_a_healthy_robot(policy, spec) -> None:
    assert policy.evaluate(spec, ActionSource.LLM, context())


def test_the_decision_is_deterministic(policy) -> None:
    """The same request against the same world is decided the same way, every time.

    Determinism is what makes the policy auditable: an incident can be replayed, and a
    rejection cannot be explained away as a timing artefact.
    """
    fixed = context(now=utcnow())
    spec = MoveAction(distance_mm=300)
    first = policy.evaluate(spec, ActionSource.LLM, fixed)
    for _ in range(200):
        again = policy.evaluate(spec, ActionSource.LLM, fixed)
        assert (again.allowed, again.reason, again.message) == (first.allowed, first.reason, first.message)


# -- the link ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_an_unregistered_robot_is_rejected(policy, source) -> None:
    decision = policy.evaluate(MoveAction(distance_mm=100), source, context(state=None))
    assert decision.reason is RejectionReason.ROBOT_UNKNOWN


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_disconnected_robot_is_rejected(policy, source) -> None:
    """Including a stop: a command cannot be delivered over a closed session, and saying
    it was accepted would be a lie the caller could act on."""
    state = robot_state(connected=False)
    for spec in (MoveAction(distance_mm=100), StopAction()):
        assert policy.evaluate(spec, source, context(state)).reason is RejectionReason.DEVICE_DISCONNECTED


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_an_action_whose_tool_the_device_does_not_publish_is_rejected(policy, source) -> None:
    """An allow-list, not a deny-list: only what discovery found is dispatchable."""
    state = robot_state(tools=["robot.motion.turn"])
    assert policy.evaluate(MoveAction(distance_mm=100), source, context(state)).reason is (
        RejectionReason.UNSUPPORTED_ACTION
    )
    assert policy.evaluate(TurnAction(angle_deg=45), source, context(state))


def test_a_robot_that_has_published_nothing_can_do_nothing(policy) -> None:
    """Before discovery finishes there is no allow-list, so there is nothing to allow."""
    state = robot_state(tools=[])
    assert policy.evaluate(StopAction(), ActionSource.SAFETY, context(state)).reason is (
        RejectionReason.UNSUPPORTED_ACTION
    )


@pytest.mark.parametrize("source", ORDINARY_SOURCES)
def test_an_expired_heartbeat_rejects_new_work(policy, source) -> None:
    """"Stop motion when heartbeat expires", at the admission end."""
    now = utcnow()
    state = robot_state(last_seen=now - timedelta(seconds=30))
    decision = policy.evaluate(MoveAction(distance_mm=100), source, context(state, now=now))
    assert decision.reason is RejectionReason.HEARTBEAT_EXPIRED
    assert "30" in decision.message


def test_a_stop_is_still_accepted_when_the_heartbeat_has_lapsed(policy) -> None:
    """Rule 1: stop is not gated on the things that gate everything else."""
    now = utcnow()
    state = robot_state(last_seen=now - timedelta(seconds=30))
    assert policy.evaluate(StopAction(), ActionSource.LLM, context(state, now=now))


# -- sensor gating ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_move_with_a_cliff_asserted_is_rejected_whoever_asked(policy, source) -> None:
    """The Phase 3 acceptance criterion, parameterized over every origin."""
    state = robot_state(sensors=sensors(cliff_detected=True))
    for spec in (MoveAction(distance_mm=200), MoveAction(distance_mm=-200), TurnAction(angle_deg=45)):
        decision = policy.evaluate(spec, source, context(state))
        assert decision.reason is RejectionReason.CLIFF_HAZARD, spec


def test_a_cliff_does_not_block_the_head_the_lift_or_the_face(policy) -> None:
    """Sensor gating is for motion. A robot at the edge of a table may still look and emote."""
    state = robot_state(sensors=sensors(cliff_detected=True))
    for spec in (LookAtAction(), LiftAction(height_pct=10), ExpressionAction(emotion="scared")):
        assert policy.evaluate(spec, ActionSource.BEHAVIOR, context(state)), spec


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_robot_that_reports_it_is_off_the_ground_does_not_drive(policy, source) -> None:
    state = robot_state(sensors=sensors(picked_up=True))
    assert policy.evaluate(MoveAction(distance_mm=100), source, context(state)).reason is (
        RejectionReason.ROBOT_LIFTED
    )


def test_stale_sensor_data_rejects_motion(policy) -> None:
    """The world model is a cache. An old cliff reading is not a cliff reading."""
    now = utcnow()
    stale = RobotSensorState(updated_at=now - timedelta(seconds=10), readings={"front_mm": 1500.0})
    decision = policy.evaluate(
        MoveAction(distance_mm=100), ActionSource.USER, context(robot_state(sensors=stale), now=now)
    )
    assert decision.reason is RejectionReason.SENSOR_DATA_STALE
    assert "10" in decision.message


def test_a_robot_that_has_never_reported_sensors_does_not_drive(policy) -> None:
    from robot.state.models import RobotTelemetry

    state = robot_state(telemetry=RobotTelemetry())
    assert policy.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context(state)).reason is (
        RejectionReason.SENSOR_DATA_MISSING
    )


def test_an_obstacle_inside_the_configured_threshold_blocks_forward_motion(policy) -> None:
    state = robot_state(sensors=sensors(readings={"front_mm": 120.0}))
    decision = policy.evaluate(MoveAction(distance_mm=300), ActionSource.LLM, context(state))
    assert decision.reason is RejectionReason.OBSTACLE_TOO_CLOSE
    assert "120" in decision.message


def test_the_obstacle_threshold_is_configurable() -> None:
    close = robot_state(sensors=sensors(readings={"front_mm": 300.0}))
    permissive = RobotSafetyPolicy(SafetyLimits(min_obstacle_distance_mm=100))
    strict = RobotSafetyPolicy(SafetyLimits(min_obstacle_distance_mm=500))
    assert permissive.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context(close))
    assert strict.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context(close)).reason is (
        RejectionReason.OBSTACLE_TOO_CLOSE
    )


def test_reversing_away_from_an_obstacle_or_a_bump_is_allowed(policy) -> None:
    """Refusing it would strand the robot against the thing it hit."""
    state = robot_state(sensors=sensors(bump_detected=True, readings={"front_mm": 30.0}))
    assert policy.evaluate(MoveAction(distance_mm=-200), ActionSource.USER, context(state))
    assert policy.evaluate(TurnAction(angle_deg=90), ActionSource.USER, context(state))
    assert policy.evaluate(MoveAction(distance_mm=200), ActionSource.USER, context(state)).reason is (
        RejectionReason.BUMP_HAZARD
    )


def test_a_flat_battery_stops_motion_unless_it_is_charging(policy) -> None:
    flat = robot_state(battery=RobotBatteryState(percent=2, charging=False))
    assert policy.evaluate(MoveAction(distance_mm=100), ActionSource.BEHAVIOR, context(flat)).reason is (
        RejectionReason.BATTERY_TOO_LOW
    )
    charging = robot_state(battery=RobotBatteryState(percent=2, charging=True))
    assert policy.evaluate(MoveAction(distance_mm=100), ActionSource.BEHAVIOR, context(charging))


# -- bounds ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_move_beyond_the_limit_is_rejected_and_not_clamped(policy, source) -> None:
    """Phase 3 acceptance: rejected with a typed reason, and the caller is told the number."""
    decision = policy.evaluate(MoveAction(distance_mm=5000), source, context())
    assert not decision.allowed
    assert decision.reason is RejectionReason.DISTANCE_LIMIT_EXCEEDED
    assert "5000" in decision.message and "1000" in decision.message


def test_the_maximum_distance_per_command_is_configurable() -> None:
    generous = RobotSafetyPolicy(SafetyLimits(max_distance_mm=2000))
    assert generous.evaluate(MoveAction(distance_mm=1800), ActionSource.USER, context())
    assert generous.evaluate(MoveAction(distance_mm=2200), ActionSource.USER, context()).reason is (
        RejectionReason.DISTANCE_LIMIT_EXCEEDED
    )


def test_the_distance_limit_applies_in_both_directions(policy) -> None:
    assert policy.evaluate(MoveAction(distance_mm=-5000), ActionSource.USER, context()).reason is (
        RejectionReason.DISTANCE_LIMIT_EXCEEDED
    )


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_turn_beyond_the_limit_is_rejected(policy, source) -> None:
    decision = policy.evaluate(TurnAction(angle_deg=540), source, context())
    assert decision.reason is RejectionReason.ANGLE_LIMIT_EXCEEDED
    assert "540" in decision.message


def test_the_maximum_turn_angle_is_configurable() -> None:
    narrow = RobotSafetyPolicy(SafetyLimits(max_angle_deg=45))
    assert narrow.evaluate(TurnAction(angle_deg=30), ActionSource.USER, context())
    assert narrow.evaluate(TurnAction(angle_deg=90), ActionSource.USER, context()).reason is (
        RejectionReason.ANGLE_LIMIT_EXCEEDED
    )


@pytest.mark.parametrize(
    "spec,reason",
    [
        (MoveAction(distance_mm=100, speed_mmps=900), RejectionReason.SPEED_LIMIT_EXCEEDED),
        (TurnAction(angle_deg=45, speed_dps=900), RejectionReason.SPEED_LIMIT_EXCEEDED),
        (MoveAction(distance_mm=0), RejectionReason.PARAMETER_OUT_OF_RANGE),
        (TurnAction(angle_deg=0), RejectionReason.PARAMETER_OUT_OF_RANGE),
        (MoveAction(distance_mm=100, speed_mmps=0), RejectionReason.PARAMETER_OUT_OF_RANGE),
        (FollowTargetAction(target_id="p", duration_ms=600_000), RejectionReason.DURATION_LIMIT_EXCEEDED),
        (AnimationAction(name="x", duration_ms=600_000), RejectionReason.DURATION_LIMIT_EXCEEDED),
        (LiftAction(height_pct=140), RejectionReason.PARAMETER_OUT_OF_RANGE),
        (LookAtAction(x_pct=-5), RejectionReason.PARAMETER_OUT_OF_RANGE),
        (ExpressionAction(emotion="happy", intensity_pct=300), RejectionReason.PARAMETER_OUT_OF_RANGE),
    ],
    ids=lambda value: getattr(value, "value", None) or type(value).__name__,
)
def test_out_of_range_parameters_are_rejected_with_the_right_reason(policy, spec, reason) -> None:
    assert policy.evaluate(spec, ActionSource.LLM, context()).reason is reason


def test_a_non_terminating_request_is_refused(policy) -> None:
    """"Every action has a deadline" is enforced, not merely documented: there is no
    "drive forward" without a distance, and no motion at zero speed."""
    for spec in (MoveAction(distance_mm=0), TurnAction(angle_deg=0), AnimationAction(name="x", duration_ms=0)):
        decision = policy.evaluate(spec, ActionSource.LLM, context())
        assert decision.reason is RejectionReason.PARAMETER_OUT_OF_RANGE
        assert "terminate" in decision.message


# -- TTL and rate --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_request_older_than_its_ttl_is_rejected(policy, source) -> None:
    """A "come here" that waited twenty seconds behind a queue is not the same request."""
    decision = policy.evaluate(MoveAction(distance_mm=100), source, context(age_s=60.0))
    assert decision.reason is RejectionReason.TTL_EXPIRED


def test_the_ttl_is_configurable() -> None:
    patient = RobotSafetyPolicy(SafetyLimits(action_ttl_s=120.0))
    assert patient.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context(age_s=60.0))


def test_the_ttl_applies_to_a_stop_as_well(policy) -> None:
    """A stop from a minute ago belongs to a situation that has already resolved."""
    assert policy.evaluate(StopAction(), ActionSource.USER, context(age_s=60.0)).reason is (
        RejectionReason.TTL_EXPIRED
    )


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_too_many_motion_commands_in_the_window_are_rejected(policy, source) -> None:
    """A looping behaviour or a confused model cannot outrun the device."""
    assert policy.evaluate(MoveAction(distance_mm=100), source, context(recent_motions=9))
    assert policy.evaluate(MoveAction(distance_mm=100), source, context(recent_motions=10)).reason is (
        RejectionReason.RATE_LIMIT_EXCEEDED
    )


def test_the_rate_limit_does_not_apply_to_the_head_or_a_stop(policy) -> None:
    assert policy.evaluate(LookAtAction(), ActionSource.LLM, context(recent_motions=99))
    assert policy.evaluate(StopAction(), ActionSource.LLM, context(recent_motions=99))


# -- the emergency stop ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ORDINARY_SOURCES)
def test_nothing_is_admitted_while_the_emergency_stop_is_engaged(policy, source) -> None:
    policy.estop.engage(ROBOT_ID, "a person is too close")
    for spec in (MoveAction(distance_mm=100), LookAtAction(), ExpressionAction(emotion="happy")):
        decision = policy.evaluate(spec, source, context())
        assert decision.reason is RejectionReason.EMERGENCY_STOP_ENGAGED
        assert "a person is too close" in decision.message


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_a_stop_is_always_accepted_even_while_latched(policy, source) -> None:
    policy.estop.engage(ROBOT_ID)
    assert policy.evaluate(StopAction(), source, context())


def test_the_safety_layer_itself_may_still_act_while_latched(policy) -> None:
    """The one asymmetry in the whole policy, and it points at stopping, not moving."""
    policy.estop.engage(ROBOT_ID)
    assert policy.evaluate(StopAction(), ActionSource.SAFETY, context())


def test_the_llm_cannot_talk_its_way_past_a_latch(policy) -> None:
    """No argument, priority or phrasing reaches a different answer: the source is data."""
    policy.estop.engage(ROBOT_ID)
    for spec in (MoveAction(distance_mm=1), MoveAction(distance_mm=-1), TurnAction(angle_deg=1)):
        assert policy.evaluate(spec, ActionSource.LLM, context()).reason is (
            RejectionReason.EMERGENCY_STOP_ENGAGED
        )


def test_work_resumes_only_after_the_latch_is_cleared_explicitly(policy) -> None:
    policy.estop.engage(ROBOT_ID)
    assert not policy.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context())
    assert policy.estop.clear(ROBOT_ID)
    assert policy.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context())
    assert not policy.estop.clear(ROBOT_ID)  # clearing twice is a no-op, not an error


def test_the_latch_is_per_robot(policy) -> None:
    policy.estop.engage(ROBOT_ID)
    other = robot_state("other-robot")
    decision = policy.evaluate(
        MoveAction(distance_mm=100),
        ActionSource.USER,
        SafetyContext(robot_id="other-robot", state=other, now=utcnow()),
    )
    assert decision.allowed
    assert policy.estop.engaged_ids() == (ROBOT_ID,)


def test_re_engaging_keeps_the_original_reason(policy) -> None:
    """The interesting fact for an incident log is what stopped the robot, not the re-assert."""
    first = policy.estop.engage(ROBOT_ID, "cliff", engaged_by="watchdog")
    again = policy.estop.engage(ROBOT_ID, "operator pressed the button", engaged_by="ui")
    assert again is first
    assert again.reason == "cliff" and again.engaged_by == "watchdog"


# -- supervisory helpers ------------------------------------------------------------------------------------------


def test_hazard_for_names_what_should_stop_a_motion_already_running(policy) -> None:
    now = utcnow()
    assert policy.hazard_for(robot_state(), now) is None
    assert policy.hazard_for(None, now) is RejectionReason.ROBOT_UNKNOWN
    assert policy.hazard_for(robot_state(connected=False), now) is RejectionReason.DEVICE_DISCONNECTED
    assert policy.hazard_for(robot_state(sensors=sensors(cliff_detected=True)), now) is (
        RejectionReason.CLIFF_HAZARD
    )
    assert policy.hazard_for(robot_state(last_seen=now - timedelta(seconds=30)), now) is (
        RejectionReason.HEARTBEAT_EXPIRED
    )


def test_hazard_for_is_narrower_than_admission(policy) -> None:
    """A robot already moving is stopped for a hazard, not for a ceiling it was admitted
    under. Re-applying the admission rules mid-motion would stop it for a rate limit."""
    close = robot_state(sensors=sensors(readings={"front_mm": 10.0}))
    assert policy.hazard_for(close) is None
    assert not policy.evaluate(MoveAction(distance_mm=100), ActionSource.USER, context(close))


def test_a_caller_supplied_timeout_is_held_inside_the_ceiling(policy) -> None:
    """The one adjustment the policy makes, and it only ever fires the watchdog sooner."""
    assert policy.clamp_timeout(5.0) == 5.0
    assert policy.clamp_timeout(600.0) == policy.limits.max_timeout_s
    assert policy.clamp_timeout(0.0) == policy.limits.default_timeout_s
    assert policy.clamp_timeout(-1.0) == policy.limits.default_timeout_s


# -- limits as configuration -----------------------------------------------------------------------------------------


def test_the_shipped_defaults_are_conservative() -> None:
    limits = SafetyLimits()
    assert limits.max_distance_mm <= 1000
    assert limits.max_angle_deg <= 180
    assert limits.max_speed_mmps <= 300
    assert limits.min_obstacle_distance_mm >= 200
    assert limits.heartbeat_timeout_s <= 10


def test_limits_are_frozen_so_a_ceiling_cannot_be_raised_at_runtime() -> None:
    from pydantic import ValidationError

    limits = SafetyLimits()
    with pytest.raises(ValidationError):
        limits.max_distance_mm = 99_999
    assert limits.with_overrides(max_distance_mm=1500).max_distance_mm == 1500
    assert limits.max_distance_mm == 1000  # the original is untouched


def test_a_limits_document_is_parsed_and_a_typo_raises() -> None:
    """Strict on purpose: a typo that was ignored would silently leave the default ceiling."""
    assert limits_from_mapping({"limits": {"max_distance_mm": 250}}).max_distance_mm == 250
    assert limits_from_mapping({"max_angle_deg": 30}).max_angle_deg == 30
    assert limits_from_mapping(None) == SafetyLimits()
    with pytest.raises(Exception):
        limits_from_mapping({"limits": {"max_distnce_mm": 250}})
    with pytest.raises(ValueError):
        limits_from_mapping([1, 2, 3])


def test_a_missing_limits_file_leaves_a_conservative_policy_not_none(tmp_path) -> None:
    assert load_limits(tmp_path / "absent.yaml") == SafetyLimits()


def test_a_limits_file_is_read(tmp_path) -> None:
    path = tmp_path / "robot_limits.yaml"
    path.write_text("limits:\n  max_distance_mm: 400\n  max_angle_deg: 60\n", encoding="utf-8")
    limits = load_limits(path)
    assert (limits.max_distance_mm, limits.max_angle_deg) == (400, 60)


def test_a_broken_limits_file_raises_rather_than_falling_back(tmp_path) -> None:
    """Falling back to defaults after an operator wrote a limits file is worse than failing."""
    path = tmp_path / "robot_limits.yaml"
    path.write_text("limits:\n  max_distance_mm: not-a-number\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_limits(path)
