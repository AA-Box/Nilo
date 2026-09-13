"""The action queue and the resource ledger: ordering, conflicts, claims, preemption.

The property this file exists to pin down is the one the whole design rests on: **two
actions cannot command the same physical subsystem at the same time**. Everything else —
priority, preemption, head-of-line behaviour — is how that constraint is scheduled around,
not a relaxation of it.

No event loop and no device: the queue never awaits, which is exactly why a conflict check
and the claim that follows it cannot interleave.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from robot.actions.model import (
    AnimationAction,
    CaptureImageAction,
    ExpressionAction,
    FollowTargetAction,
    LiftAction,
    LookAtAction,
    MoveAction,
    RobotAction,
    StopAction,
    TurnAction,
)
from robot.actions.queue import ResourceConflict, RobotActionQueue, unique
from robot.actions.registry import RobotActionRegistry
from robot.state.actions import ActionPriority, ActionStatus, Resource
from robot.state.models import utcnow

ROBOT = "test-robot"
OTHER = "other-robot"


def act(spec=None, *, robot_id: str = ROBOT, priority: ActionPriority = ActionPriority.NORMAL, age_s: float = 0.0):
    return RobotAction(
        spec or MoveAction(distance_mm=100),
        robot_id,
        priority=priority,
        created_at=utcnow() - timedelta(seconds=age_s),
    )


@pytest.fixture
def queue() -> RobotActionQueue:
    return RobotActionQueue()


# -- ordering ------------------------------------------------------------------------------------


def test_the_queue_dispatches_the_highest_priority_first(queue) -> None:
    low = act(priority=ActionPriority.LOW)
    high = act(priority=ActionPriority.HIGH)
    normal = act(priority=ActionPriority.NORMAL)
    for item in (low, normal, high):
        queue.enqueue(item)
    assert [item.priority for item in queue.pending(ROBOT)] == [
        ActionPriority.HIGH, ActionPriority.NORMAL, ActionPriority.LOW
    ]
    assert queue.next_ready(ROBOT) is high


def test_equal_priorities_are_first_in_first_out(queue) -> None:
    older = act(age_s=5)
    newer = act(age_s=1)
    queue.enqueue(newer)
    queue.enqueue(older)
    assert queue.next_ready(ROBOT) is older


def test_each_robot_has_its_own_queue(queue) -> None:
    mine, yours = act(), act(robot_id=OTHER)
    queue.enqueue(mine)
    queue.enqueue(yours)
    assert queue.pending(ROBOT) == (mine,)
    assert queue.pending(OTHER) == (yours,)
    assert set(queue.robot_ids()) == {ROBOT, OTHER}
    assert len(queue) == 2


def test_removing_the_last_pending_action_forgets_the_robots_queue(queue) -> None:
    item = act()
    queue.enqueue(item)
    assert queue.remove(item.action_id) is item
    assert queue.robot_ids() == ()
    assert queue.remove(item.action_id) is None


# -- claims and conflicts ---------------------------------------------------------------------------


def test_claiming_a_resource_twice_raises_rather_than_double_booking(queue) -> None:
    first, second = act(MoveAction(distance_mm=100)), act(TurnAction(angle_deg=90))
    queue.claim(first)
    with pytest.raises(ResourceConflict) as raised:
        queue.claim(second)
    assert raised.value.resource is Resource.DRIVE
    assert raised.value.holder_id == first.action_id
    assert queue.holder(ROBOT, Resource.DRIVE) is first


def test_conflicts_name_the_holder_not_merely_that_there_is_one(queue) -> None:
    running = act(MoveAction(distance_mm=100))
    queue.claim(running)
    blocked = act(TurnAction(angle_deg=90))
    assert queue.conflicts(blocked) == (running,)
    assert not queue.is_free(blocked)
    assert queue.is_free(act(LookAtAction()))  # a different subsystem is unaffected


def test_a_follow_conflicts_with_drive_head_and_camera_work_at_once(queue) -> None:
    follow = act(FollowTargetAction(target_id="person-1"))
    queue.claim(follow)
    assert queue.claimed(ROBOT) == frozenset({Resource.DRIVE, Resource.HEAD, Resource.CAMERA})
    for spec in (MoveAction(distance_mm=50), LookAtAction(), CaptureImageAction()):
        assert queue.conflicts(act(spec)) == (follow,)
    assert queue.is_free(act(LiftAction(height_pct=50)))  # the lift is untouched


def test_an_expression_and_an_animation_contend_for_the_one_face(queue) -> None:
    expression = act(ExpressionAction(emotion="happy"))
    queue.claim(expression)
    assert queue.conflicts(act(AnimationAction(name="greet"))) == (expression,)


def test_claims_are_per_robot(queue) -> None:
    mine = act(MoveAction(distance_mm=100))
    queue.claim(mine)
    assert queue.is_free(act(MoveAction(distance_mm=100), robot_id=OTHER))


def test_re_claiming_by_the_same_action_is_not_a_conflict(queue) -> None:
    item = act()
    queue.claim(item)
    queue.claim(item)
    assert queue.running(ROBOT) == (item,)


def test_releasing_gives_back_exactly_what_was_held_and_is_idempotent(queue) -> None:
    follow = act(FollowTargetAction(target_id="person-1"))
    queue.claim(follow)
    freed = queue.release(follow)
    assert set(freed) == {Resource.DRIVE, Resource.HEAD, Resource.CAMERA}
    assert queue.release(follow) == ()
    assert queue.claimed(ROBOT) == frozenset()
    assert queue.running(ROBOT) == ()


def test_releasing_one_action_does_not_free_anothers_claim(queue) -> None:
    drive = act(MoveAction(distance_mm=100))
    head = act(LookAtAction())
    queue.claim(drive)
    queue.claim(head)
    queue.release(drive)
    assert queue.claimed(ROBOT) == frozenset({Resource.HEAD})
    assert queue.running(ROBOT) == (head,)


# -- selection ----------------------------------------------------------------------------------------


def test_next_ready_skips_a_blocked_head_for_work_on_a_free_subsystem(queue) -> None:
    """Head-of-line blocking applies only to the subsystem actually contended."""
    queue.claim(act(MoveAction(distance_mm=100)))
    blocked_drive = act(TurnAction(angle_deg=90), priority=ActionPriority.HIGH)
    free_head = act(LookAtAction(), priority=ActionPriority.LOW)
    queue.enqueue(blocked_drive)
    queue.enqueue(free_head)
    assert queue.next_ready(ROBOT) is free_head
    assert queue.next_blocked(ROBOT) is blocked_drive


def test_next_blocked_is_none_when_the_head_can_run(queue) -> None:
    queue.enqueue(act())
    assert queue.next_blocked(ROBOT) is None
    assert queue.next_ready(ROBOT) is not None


def test_an_empty_queue_offers_nothing(queue) -> None:
    assert queue.next_ready(ROBOT) is None
    assert queue.next_blocked(ROBOT) is None
    assert queue.pending(ROBOT) == ()


# -- preemption ---------------------------------------------------------------------------------------


def test_a_higher_priority_action_can_preempt_a_lower_one(queue) -> None:
    """The user says "come here" while a behaviour is looking around: the user wins."""
    behaviour = act(FollowTargetAction(target_id="person-1"), priority=ActionPriority.LOW)
    queue.claim(behaviour)
    user = act(MoveAction(distance_mm=300), priority=ActionPriority.HIGH)
    queue.enqueue(user)
    assert queue.preemptable(user) == (behaviour,)


def test_an_equal_priority_action_waits_instead_of_preempting(queue) -> None:
    """Strictly greater, so two peers queue behind each other instead of cancelling in a loop."""
    running = act(MoveAction(distance_mm=100), priority=ActionPriority.NORMAL)
    queue.claim(running)
    peer = act(TurnAction(angle_deg=90), priority=ActionPriority.NORMAL)
    queue.enqueue(peer)
    assert queue.preemptable(peer) == ()
    assert queue.conflicts(peer) == (running,)


def test_a_lower_priority_action_never_preempts(queue) -> None:
    queue.claim(act(MoveAction(distance_mm=100), priority=ActionPriority.HIGH))
    assert queue.preemptable(act(TurnAction(angle_deg=90), priority=ActionPriority.LOW)) == ()


def test_an_emergency_stop_outranks_everything_that_can_hold_the_drive(queue) -> None:
    for priority in (ActionPriority.IDLE, ActionPriority.LOW, ActionPriority.NORMAL, ActionPriority.HIGH):
        fresh = RobotActionQueue()
        fresh.claim(act(MoveAction(distance_mm=100), priority=priority))
        stop = act(StopAction(), priority=ActionPriority.EMERGENCY)
        assert len(fresh.preemptable(stop)) == 1


# -- bulk operations --------------------------------------------------------------------------------------


def test_draining_empties_the_pending_queue_and_leaves_claims_alone(queue) -> None:
    """What cancel_all and the emergency stop empty. A running action owns its claim until
    it is settled, so draining must not silently free it."""
    running = act(MoveAction(distance_mm=100))
    queue.claim(running)
    queued = [act(TurnAction(angle_deg=90)), act(LookAtAction())]
    for item in queued:
        queue.enqueue(item)
    drained = queue.drain(ROBOT)
    assert set(drained) == set(queued)
    assert queue.pending(ROBOT) == ()
    assert queue.running(ROBOT) == (running,)


def test_draining_every_robot_leaves_no_queue_behind(queue) -> None:
    queue.enqueue(act())
    queue.enqueue(act(robot_id=OTHER))
    assert len(queue.drain()) == 2
    assert len(queue) == 0
    assert queue.robot_ids() == ()


def test_forgetting_a_robot_returns_everything_it_had(queue) -> None:
    running = act(MoveAction(distance_mm=100))
    pending = act(LookAtAction())
    queue.claim(running)
    queue.enqueue(pending)
    forgotten = queue.forget(ROBOT)
    assert set(forgotten) == {running, pending}
    assert queue.claimed(ROBOT) == frozenset()
    assert queue.pending(ROBOT) == ()


def test_unique_collapses_an_action_that_appears_twice() -> None:
    """A drained queue plus the running set can name the same action twice."""
    item, other = act(), act(LookAtAction())
    assert unique([item, item, other, item]) == (item, other)


# -- the invariant, under a long random sequence -------------------------------------------------------------


def test_no_sequence_of_claims_and_releases_double_books_a_subsystem() -> None:
    """A thousand interleaved claims and releases; the ledger never holds two owners.

    This is the resource half of the Phase 3 acceptance criterion: "no sequence of events
    ... leaks a resource claim". The registry is driven alongside so a leak shows up as a
    live action with no claim, or a claim with no live action.
    """
    import random

    rng = random.Random(913)
    queue = RobotActionQueue()
    registry = RobotActionRegistry()
    specs = [
        MoveAction(distance_mm=100), TurnAction(angle_deg=45), LookAtAction(),
        LiftAction(height_pct=50), ExpressionAction(emotion="happy"),
        CaptureImageAction(), FollowTargetAction(target_id="person-1"),
    ]
    live: list[RobotAction] = []
    for _ in range(1000):
        if live and rng.random() < 0.45:
            victim = live.pop(rng.randrange(len(live)))
            queue.release(victim)
            victim.transition(ActionStatus.CANCELLED)
            registry.retire(victim)
        else:
            candidate = act(rng.choice(specs), robot_id=rng.choice([ROBOT, OTHER]))
            registry.add(candidate)
            if queue.is_free(candidate):
                queue.claim(candidate)
                candidate.transition(ActionStatus.STARTING)
                live.append(candidate)
            else:
                queue.enqueue(candidate)
        for robot_id in (ROBOT, OTHER):
            holders = queue.running(robot_id)
            claimed = [resource for holder in holders for resource in holder.resources]
            assert len(claimed) == len(set(claimed)), "a subsystem is held by two actions"
            for holder in holders:
                assert not holder.is_terminal, "a terminal action still holds a claim"
