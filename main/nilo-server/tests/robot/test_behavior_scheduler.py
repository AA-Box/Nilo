"""Arbitration: scoring, priority bands, cooldowns, conflicts, preemption, determinism.

Everything here runs on a fake clock and a seeded RNG. Nothing sleeps, nothing polls and
nothing reads the wall clock — a behaviour test that flakes is worse than no test
(docs/robot-roadmap.md Phase 6).
"""
from __future__ import annotations

import asyncio

import pytest

from robot.behavior.base import (
    AutonomyMode,
    Behavior,
    BehaviorCategory,
    BehaviorContext,
    BehaviorPriority,
    BehaviorResult,
)
from robot.behavior.scheduler import BehaviorRegistry, BehaviorScheduler
from robot.behavior.tuning import BehaviorTuning
from robot.events.bus import EventBus
from robot.events.types import (
    BehaviorCompleted,
    BehaviorEvaluated,
    BehaviorInterrupted,
    BehaviorSelected,
    BehaviorStarted,
)
from robot.state.actions import Resource
from tests.robot.conftest import ROBOT_ID, FakeClock, RecordingRobot, behavior_world, make_scheduler

NO_JITTER = BehaviorTuning(score_jitter=0.0)


class Fixed(Behavior):
    """A behaviour whose score, band and resources a test sets directly."""

    def __init__(
        self,
        name: str,
        score: float,
        *,
        priority: BehaviorPriority = BehaviorPriority.NORMAL,
        resources: frozenset[Resource] = frozenset(),
        cooldown_s: float = 0.0,
        runnable: bool = True,
        interruptible: bool = True,
        min_runtime_s: float | None = None,
        max_runtime_s: float | None = None,
        min_autonomy: AutonomyMode = AutonomyMode.NORMAL,
        hold: bool = False,
    ) -> None:
        self.name = name
        self.value = score
        self.priority = priority
        self.required_resources = resources
        self.cooldown_s = cooldown_s
        self.runnable = runnable
        self.interruptible = interruptible
        self.min_runtime_s = min_runtime_s
        self.max_runtime_s = max_runtime_s
        self.min_autonomy = min_autonomy
        self.category = BehaviorCategory.SOCIAL
        self.hold = hold
        self.ran = 0
        self.cancelled = 0

    def can_run(self, context: BehaviorContext) -> bool:
        return self.runnable

    def score(self, context: BehaviorContext) -> float:
        context.because(f"{self.name} scores {self.value}")
        return self.value

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        self.ran += 1
        if self.hold:
            await asyncio.Event().wait()  # runs until cancelled
        return BehaviorResult.completed(f"{self.name} done")

    async def cancel(self) -> None:
        self.cancelled += 1


async def settle() -> None:
    """Let the behaviour task reach its first await. One turn is enough; none of the
    behaviours here do I/O."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# -- selection --------------------------------------------------------------------------------


def test_the_highest_utility_in_the_same_band_wins():
    scheduler, _, _ = make_scheduler(Fixed("a", 0.3), Fixed("b", 0.7), tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world())
    assert decision.selected == "b"
    assert decision.score == pytest.approx(0.7)


def test_a_higher_band_beats_a_higher_score():
    """The categorical override: safety and system are not compared on utility."""
    scheduler, _, _ = make_scheduler(
        Fixed("social", 0.99),
        Fixed("power", 0.20, priority=BehaviorPriority.CRITICAL),
        tuning=NO_JITTER,
    )
    assert scheduler.evaluate(behavior_world()).selected == "power"


def test_nothing_is_selected_when_everything_is_below_the_floor():
    tuning = BehaviorTuning(score_jitter=0.0, min_score=0.5)
    scheduler, _, _ = make_scheduler(Fixed("a", 0.1), tuning=tuning)
    decision = scheduler.evaluate(behavior_world())
    assert decision.selected is None
    assert "floor" in decision.note


def test_can_run_filters_before_scoring_and_says_so():
    scheduler, _, _ = make_scheduler(Fixed("a", 0.9, runnable=False), Fixed("b", 0.1), tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world())
    assert decision.selected == "b"
    rejected = {c.name: c.rejected_because for c in decision.candidates if not c.eligible}
    assert rejected == {"a": "can_run said no"}


def test_a_behaviour_that_raises_while_scoring_is_skipped_not_fatal():
    class Broken(Fixed):
        def score(self, context: BehaviorContext) -> float:
            raise ValueError("bad maths")

    scheduler, _, _ = make_scheduler(Broken("broken", 0.9), Fixed("ok", 0.2), tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world())
    assert decision.selected == "ok"
    assert any(c.name == "broken" and not c.eligible for c in decision.candidates)


# -- determinism ---------------------------------------------------------------------------------


def test_the_same_world_and_seed_select_the_same_behaviour_a_thousand_times():
    """The headline determinism guarantee of the phase."""
    world = behavior_world()
    selections = set()
    for _ in range(1000):
        scheduler, _, _ = make_scheduler(
            Fixed("a", 0.50), Fixed("b", 0.52), Fixed("c", 0.51), seed=1234
        )
        selections.add(scheduler.evaluate(world).selected)
    assert selections == {"b"}


def test_jitter_is_a_function_of_seed_tick_and_name_only():
    world = behavior_world()
    first, _, _ = make_scheduler(Fixed("a", 0.5), seed=7)
    second, _, _ = make_scheduler(Fixed("a", 0.5), seed=7)
    third, _, _ = make_scheduler(Fixed("a", 0.5), seed=8)
    a = first.evaluate(world).score
    b = second.evaluate(world).score
    c = third.evaluate(world).score
    assert a == b
    assert a != c  # a different seed is a different robot


def test_two_different_seeds_can_break_a_tie_differently():
    """Seeded variation exists so identical options are not always resolved identically."""
    world = behavior_world()
    winners = set()
    for seed in range(40):
        scheduler, _, _ = make_scheduler(Fixed("a", 0.5), Fixed("b", 0.5), seed=seed)
        winners.add(scheduler.evaluate(world).selected)
    assert winners == {"a", "b"}


def test_registration_order_does_not_change_the_answer():
    world = behavior_world()
    forwards = BehaviorRegistry([Fixed("a", 0.4), Fixed("b", 0.6)])
    backwards = BehaviorRegistry([Fixed("b", 0.6), Fixed("a", 0.4)])
    robot = RecordingRobot()
    one = BehaviorScheduler(ROBOT_ID, robot, registry=forwards, tuning=NO_JITTER, clock=FakeClock())
    two = BehaviorScheduler(ROBOT_ID, robot, registry=backwards, tuning=NO_JITTER, clock=FakeClock())
    assert one.evaluate(world).selected == two.evaluate(world).selected == "b"


# -- cooldowns ------------------------------------------------------------------------------------


async def test_a_behaviour_in_cooldown_is_not_a_candidate():
    scheduler, _, clock = make_scheduler(
        Fixed("chatty", 0.9, cooldown_s=10.0), Fixed("quiet", 0.1), tuning=NO_JITTER
    )
    assert (await scheduler.tick(behavior_world())).selected == "chatty"
    await settle()
    clock.advance(1.0)
    decision = await scheduler.tick(behavior_world())
    assert decision.selected == "quiet"
    assert "cooling down" in dict(
        (c.name, c.rejected_because) for c in decision.candidates
    )["chatty"]
    clock.advance(20.0)
    assert (await scheduler.tick(behavior_world())).selected == "chatty"


# -- resource conflicts ------------------------------------------------------------------------------


async def test_a_running_behaviour_blocks_one_that_wants_the_same_resource():
    drive_a = Fixed("drive_a", 0.9, resources=frozenset({Resource.DRIVE}), hold=True)
    drive_b = Fixed("drive_b", 0.8, resources=frozenset({Resource.DRIVE}))
    head = Fixed("head", 0.7, resources=frozenset({Resource.HEAD}))
    scheduler, _, clock = make_scheduler(drive_a, drive_b, head, tuning=NO_JITTER)
    await scheduler.tick(behavior_world())
    await settle()
    clock.advance(1.0)
    decision = await scheduler.tick(behavior_world())
    blocked = dict((c.name, c.rejected_because) for c in decision.candidates if not c.eligible)
    assert "drive_a holds drive" == blocked["drive_b"]
    assert "head" not in blocked
    await scheduler.aclose()


async def test_resources_that_do_not_overlap_do_not_conflict():
    display = Fixed("display", 0.9, resources=frozenset({Resource.DISPLAY}), hold=True)
    drive = Fixed("drive", 0.4, resources=frozenset({Resource.DRIVE}))
    scheduler, _, clock = make_scheduler(display, drive, tuning=NO_JITTER)
    await scheduler.tick(behavior_world())
    await settle()
    clock.advance(1.0)
    decision = await scheduler.tick(behavior_world())
    assert all(c.eligible for c in decision.candidates)
    await scheduler.aclose()


# -- preemption and runtime bounds ------------------------------------------------------------------------


async def test_a_much_better_candidate_preempts_a_running_one():
    running = Fixed("running", 0.30, hold=True, min_runtime_s=0.0)
    challenger = Fixed("challenger", 0.90)
    scheduler, _, clock = make_scheduler(running, challenger, tuning=NO_JITTER)
    # The challenger is filtered out of the first tick so the weaker one starts first.
    challenger.runnable = False
    await scheduler.tick(behavior_world())
    await settle()
    challenger.runnable = True
    clock.advance(1.0)
    decision = await scheduler.tick(behavior_world())
    assert decision.selected == "challenger"
    assert decision.preempted == "running"
    assert running.cancelled == 1
    await scheduler.aclose()


async def test_a_marginally_better_candidate_does_not_thrash_the_running_one():
    running = Fixed("running", 0.50, hold=True, min_runtime_s=0.0)
    challenger = Fixed("challenger", 0.55)
    scheduler, _, clock = make_scheduler(running, challenger, tuning=NO_JITTER)
    challenger.runnable = False
    await scheduler.tick(behavior_world())
    await settle()
    challenger.runnable = True
    clock.advance(1.0)
    decision = await scheduler.tick(behavior_world())
    assert decision.preempted is None
    assert "does not beat" in decision.note
    assert running.cancelled == 0
    await scheduler.aclose()


async def test_the_minimum_runtime_protects_a_behaviour_that_just_started():
    running = Fixed("running", 0.30, hold=True, min_runtime_s=5.0)
    challenger = Fixed("challenger", 0.95)
    scheduler, _, clock = make_scheduler(running, challenger, tuning=NO_JITTER)
    challenger.runnable = False
    await scheduler.tick(behavior_world())
    await settle()
    challenger.runnable = True
    clock.advance(1.0)
    assert "minimum runtime" in (await scheduler.tick(behavior_world())).note
    clock.advance(10.0)
    assert (await scheduler.tick(behavior_world())).preempted == "running"
    await scheduler.aclose()


async def test_an_uninterruptible_behaviour_is_only_preempted_by_a_higher_band():
    running = Fixed("running", 0.30, hold=True, interruptible=False, min_runtime_s=0.0)
    social = Fixed("social", 0.99)
    power = Fixed("power", 0.10, priority=BehaviorPriority.CRITICAL)
    scheduler, _, clock = make_scheduler(running, social, power, tuning=NO_JITTER)
    social.runnable = power.runnable = False
    await scheduler.tick(behavior_world())
    await settle()
    social.runnable = True
    clock.advance(1.0)
    assert "not interruptible" in (await scheduler.tick(behavior_world())).note
    power.runnable = True
    clock.advance(1.0)
    assert (await scheduler.tick(behavior_world())).preempted == "running"
    await scheduler.aclose()


async def test_a_behaviour_that_overruns_its_maximum_is_cancelled():
    forever = Fixed("forever", 0.9, hold=True, max_runtime_s=5.0, min_runtime_s=0.0)
    scheduler, _, clock = make_scheduler(forever, tuning=NO_JITTER)
    await scheduler.tick(behavior_world())
    await settle()
    clock.advance(6.0)
    await scheduler.tick(behavior_world())
    assert scheduler.running is None
    assert forever.cancelled == 1
    await scheduler.aclose()


async def test_a_finished_behaviour_is_reaped_and_can_run_again():
    once = Fixed("once", 0.9)
    scheduler, _, clock = make_scheduler(once, tuning=NO_JITTER)
    await scheduler.tick(behavior_world())
    await settle()
    clock.advance(1.0)
    await scheduler.tick(behavior_world())
    await settle()
    assert once.ran == 2
    await scheduler.aclose()


# -- events ---------------------------------------------------------------------------------------------


async def test_the_full_decision_reaches_the_bus_as_structured_debug_events():
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(
        (BehaviorEvaluated, BehaviorSelected, BehaviorStarted, BehaviorCompleted, BehaviorInterrupted),
        seen.append,
    )
    scheduler, _, _ = make_scheduler(
        Fixed("winner", 0.9), Fixed("loser", 0.2), tuning=NO_JITTER, events=bus
    )
    await scheduler.tick(behavior_world())
    await settle()
    await scheduler.tick(behavior_world())  # reaps the finished behaviour
    await bus.drain()

    kinds = [type(event).__name__ for event in seen]
    assert kinds[:3] == ["BehaviorEvaluated", "BehaviorSelected", "BehaviorStarted"]
    assert "BehaviorCompleted" in kinds

    evaluated = next(e for e in seen if isinstance(e, BehaviorEvaluated))
    assert dict(evaluated.scores)["winner"] == pytest.approx(0.9)
    assert evaluated.selected == "winner"
    selected = next(e for e in seen if isinstance(e, BehaviorSelected))
    assert selected.alternatives == (("loser", 0.2),)
    assert selected.reasons == ("winner scores 0.9",)
    await scheduler.aclose()
    await bus.aclose()


async def test_an_interruption_is_published_with_what_took_over():
    bus = EventBus()
    interruptions: list[BehaviorInterrupted] = []
    bus.subscribe(BehaviorInterrupted, interruptions.append)
    running = Fixed("running", 0.3, hold=True, min_runtime_s=0.0)
    challenger = Fixed("challenger", 0.9)
    scheduler, _, clock = make_scheduler(running, challenger, tuning=NO_JITTER, events=bus)
    challenger.runnable = False
    await scheduler.tick(behavior_world())
    await settle()
    challenger.runnable = True
    clock.advance(2.0)
    await scheduler.tick(behavior_world())
    await bus.drain()
    assert [(e.behavior, e.by, e.reason) for e in interruptions] == [
        ("running", "challenger", "preempted")
    ]
    assert interruptions[0].duration_s == pytest.approx(2.0)
    await scheduler.aclose()
    await bus.aclose()


# -- explaining ------------------------------------------------------------------------------------------------


def test_the_decision_explains_itself_in_the_documented_shape():
    scheduler, _, _ = make_scheduler(
        Fixed("greet_person", 0.84), Fixed("look_around", 0.31), Fixed("explore", 0.25), tuning=NO_JITTER
    )
    text = scheduler.evaluate(behavior_world()).explain()
    assert text.splitlines()[0] == "selected: greet_person"
    assert "score: 0.84" in text
    assert "look_around: 0.31" in text
    assert "explore: 0.25" in text
    assert "greet_person scores 0.84" in text


def test_the_decision_serializes_for_an_api():
    scheduler, _, _ = make_scheduler(Fixed("a", 0.8), Fixed("b", 0.2), tuning=NO_JITTER)
    data = scheduler.evaluate(behavior_world()).as_dict()
    assert data["selected"] == "a"
    assert data["alternatives"] == {"b": 0.2}
    assert data["mode"] == "normal"
    assert isinstance(data["reasons"], list)


async def test_closing_the_scheduler_stops_the_behaviour_in_flight():
    forever = Fixed("forever", 0.9, hold=True)
    scheduler, _, _ = make_scheduler(forever, tuning=NO_JITTER)
    await scheduler.tick(behavior_world())
    await settle()
    await scheduler.aclose()
    assert scheduler.running is None
    assert forever.cancelled == 1
    with pytest.raises(RuntimeError):
        await scheduler.tick(behavior_world())


# -- the registry -------------------------------------------------------------------------------------------------


def test_the_registry_refuses_a_duplicate_name_but_replace_is_explicit():
    registry = BehaviorRegistry([Fixed("a", 0.1)])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Fixed("a", 0.2))
    registry.replace(Fixed("a", 0.2))
    behavior = registry.get("a")
    assert isinstance(behavior, Fixed) and behavior.value == 0.2
    assert registry.unregister("a") is True
    assert len(registry) == 0
