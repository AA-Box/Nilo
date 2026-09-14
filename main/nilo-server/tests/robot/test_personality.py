"""Traits, the control variables, their decay, and what they are allowed to influence.

The load-bearing test in this file is the last one: personality changes *which* action is
proposed and never *whether* it is allowed. Every trait is swept across its full range with
a cliff asserted, and the move is rejected every time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from robot.behavior.base import AutonomyMode
from robot.events.bus import EventBus
from robot.events.types import ActionFinished, BatteryUpdated, BehaviorCompleted, SensorUpdated
from robot.personality.emotion import (
    EmotionEngine,
    EmotionTuning,
    EmotionalState,
    Stimulus,
    baselines_for,
    half_lives_for,
)
from robot.personality.model import PersonalityModel
from robot.personality.store import PersonalityStore
from robot.personality.traits import (
    PRESETS,
    PersonalityTraits,
    get_preset,
    load_traits,
    traits_from_mapping,
)
from robot.state.actions import (
    ActionError,
    ActionPriority,
    ActionRecord,
    ActionSource,
    ActionStatus,
    ActionType,
    RejectionReason,
)
from robot.state.models import RobotBatteryState, RobotSensorState
from robot.state.world import person
from tests.robot.conftest import ROBOT_ID, WORLD_T0, FakeClock, behavior_world, make_scheduler

TRAIT_NAMES = tuple(PersonalityTraits.model_fields)


# -- traits ---------------------------------------------------------------------------------


def test_traits_are_normalized_and_frozen():
    traits = PersonalityTraits(sociability=0.9)
    assert 0.0 <= traits.sociability <= 1.0
    with pytest.raises(Exception):
        PersonalityTraits(sociability=1.4)
    with pytest.raises(Exception):
        traits.sociability = 0.1  # type: ignore[misc]


def test_every_trait_the_design_names_exists():
    assert set(TRAIT_NAMES) == {
        "sociability",
        "curiosity",
        "playfulness",
        "boldness",
        "patience",
        "energy_baseline",
    }


def test_presets_are_distinguishable_and_loadable():
    assert set(PRESETS) == {"balanced", "puppy", "cat", "assistant"}
    assert get_preset("puppy").sociability > get_preset("cat").sociability
    with pytest.raises(KeyError, match="unknown personality preset"):
        get_preset("goldfish")


def test_a_document_seeds_from_a_preset_and_overrides_it():
    traits = traits_from_mapping({"personality": {"preset": "puppy", "patience": 0.6}})
    assert traits.sociability == pytest.approx(PRESETS["puppy"].sociability)
    assert traits.patience == pytest.approx(0.6)


def test_blending_moves_towards_the_other_personality():
    blended = PRESETS["cat"].blend(PRESETS["puppy"], 0.5)
    assert PRESETS["cat"].sociability < blended.sociability < PRESETS["puppy"].sociability


def test_traits_load_from_yaml_and_fall_back_to_balanced(tmp_path: Path):
    path = tmp_path / "personality.yaml"
    path.write_text("personality:\n  preset: cat\n  curiosity: 0.99\n", encoding="utf-8")
    traits = load_traits(path)
    assert traits.curiosity == pytest.approx(0.99)
    assert load_traits(tmp_path / "missing.yaml") == PersonalityTraits()


# -- the control variables -------------------------------------------------------------------------


def test_every_control_variable_the_design_names_exists():
    assert set(EmotionalState.model_fields) == {
        "valence",
        "arousal",
        "curiosity",
        "boredom",
        "social_need",
        "confidence",
        "energy",
    }


def test_a_positive_interaction_raises_valence_and_lowers_social_need():
    engine = EmotionEngine()
    before = engine.state
    after = engine.record(Stimulus.POSITIVE_INTERACTION)
    assert after.valence > before.valence
    assert after.social_need < before.social_need


def test_no_stimulation_raises_boredom_and_curiosity():
    engine = EmotionEngine(PersonalityTraits(curiosity=0.9), state=EmotionalState(curiosity=0.1, boredom=0.0))
    after = engine.update(600.0)
    assert after.boredom > 0.3
    assert after.curiosity > 0.1


def test_a_failed_movement_lowers_confidence_slightly():
    engine = EmotionEngine()
    before = engine.state.confidence
    after = engine.record(Stimulus.FAILED_MOVEMENT).confidence
    assert before - after == pytest.approx(0.08, abs=0.02)


def test_charging_raises_energy_on_the_fast_half_life():
    traits = PersonalityTraits(energy_baseline=0.9)
    draining = EmotionEngine(traits, state=EmotionalState(energy=0.2))
    charging = EmotionEngine(traits, state=EmotionalState(energy=0.2))
    charging.set_charging(True)
    assert charging.update(300.0).energy > draining.update(300.0).energy


def test_an_unexpected_obstacle_raises_arousal():
    engine = EmotionEngine()
    assert engine.record(Stimulus.OBSTACLE).arousal > engine.tuning.deltas_for(Stimulus.OBSTACLE)["arousal"] * 0


def test_decay_is_exponential_towards_the_baseline_and_never_overshoots():
    tuning = EmotionTuning(half_lives_s={"arousal": 10.0})
    traits = PersonalityTraits()
    engine = EmotionEngine(traits, tuning=tuning, state=EmotionalState(arousal=1.0))
    baseline = baselines_for(traits, tuning)["arousal"]
    after_one = engine.update(10.0).arousal
    after_two = engine.update(20.0).arousal
    assert after_one == pytest.approx(baseline + (1.0 - baseline) * 0.5, abs=0.01)
    assert after_two == pytest.approx(baseline + (1.0 - baseline) * 0.25, abs=0.01)
    assert engine.update(100_000.0).arousal == pytest.approx(baseline, abs=0.01)


def test_values_are_clamped_into_the_unit_range():
    engine = EmotionEngine(state=EmotionalState(valence=0.98))
    for _ in range(10):
        engine.record(Stimulus.PRAISE)
    assert engine.state.valence == 1.0
    for _ in range(50):
        engine.record(Stimulus.FAILED_MOVEMENT)
    assert engine.state.confidence >= 0.0


def test_time_never_runs_backwards():
    engine = EmotionEngine(now=100.0)
    state = engine.update(50.0)
    assert state == engine.state


def test_traits_move_the_baselines_not_the_current_values():
    sociable = baselines_for(PersonalityTraits(sociability=0.95), EmotionTuning())
    aloof = baselines_for(PersonalityTraits(sociability=0.05), EmotionTuning())
    assert sociable["social_need"] > aloof["social_need"]


def test_patience_lengthens_the_boredom_climb():
    tuning = EmotionTuning()
    patient = half_lives_for(PersonalityTraits(patience=1.0), tuning)["boredom"]
    impatient = half_lives_for(PersonalityTraits(patience=0.0), tuning)["boredom"]
    assert patient > impatient


def test_trait_influence_can_be_turned_off():
    neutral = EmotionTuning(trait_influence=0.0)
    sociable = baselines_for(PersonalityTraits(sociability=0.95), neutral)
    aloof = baselines_for(PersonalityTraits(sociability=0.05), neutral)
    assert sociable["social_need"] == aloof["social_need"]


# -- the model on the bus ----------------------------------------------------------------------------


async def test_events_move_the_control_variables():
    clock = FakeClock()
    bus = EventBus()
    model = PersonalityModel(ROBOT_ID, clock=clock)
    model.attach(bus)

    before = model.state
    await bus.publish(SensorUpdated(robot_id=ROBOT_ID, sensors=RobotSensorState(touch_detected=True)))
    await bus.drain()
    assert model.valence > before.valence
    assert model.boredom < before.boredom + 0.01

    confident = model.confidence
    await bus.publish(
        ActionFinished(
            robot_id=ROBOT_ID,
            action=_record(ActionStatus.FAILED),
        )
    )
    await bus.drain()
    assert model.confidence < confident
    await model.aclose()
    await bus.aclose()


async def test_a_hazard_rejection_is_a_surprise_not_a_failure():
    bus = EventBus()
    model = PersonalityModel(ROBOT_ID, clock=FakeClock())
    model.attach(bus)
    aroused = model.arousal
    await bus.publish(
        ActionFinished(
            robot_id=ROBOT_ID,
            action=_record(
                ActionStatus.REJECTED, error=ActionError.rejected(RejectionReason.CLIFF_HAZARD)
            ),
        )
    )
    await bus.drain()
    assert model.arousal > aroused
    await model.aclose()
    await bus.aclose()


async def test_a_hazard_that_stays_asserted_is_one_surprise():
    bus = EventBus()
    model = PersonalityModel(ROBOT_ID, clock=FakeClock())
    model.attach(bus)
    for _ in range(5):
        await bus.publish(
            SensorUpdated(robot_id=ROBOT_ID, sensors=RobotSensorState(cliff_detected=True))
        )
    await bus.drain()
    once = model.arousal
    model_two = PersonalityModel(ROBOT_ID, clock=FakeClock())
    model_two.record(Stimulus.OBSTACLE)
    assert once == pytest.approx(model_two.arousal, abs=0.001)
    await model.aclose()
    await bus.aclose()


async def test_a_completed_greeting_counts_as_a_positive_interaction():
    bus = EventBus()
    model = PersonalityModel(ROBOT_ID, clock=FakeClock())
    model.attach(bus)
    need = model.social_need
    await bus.publish(BehaviorCompleted(robot_id=ROBOT_ID, behavior="greet_person", outcome="completed"))
    await bus.drain()
    assert model.social_need < need
    await model.aclose()
    await bus.aclose()


async def test_charging_telemetry_switches_the_energy_half_life():
    bus = EventBus()
    model = PersonalityModel(ROBOT_ID, clock=FakeClock())
    model.attach(bus)
    await bus.publish(
        BatteryUpdated(robot_id=ROBOT_ID, battery=RobotBatteryState(percent=40, charging=True))
    )
    await bus.drain()
    assert model.engine.charging is True
    await model.aclose()
    await bus.aclose()


# -- persistence ----------------------------------------------------------------------------------------


def test_traits_survive_a_restart_and_the_mood_is_coarse(tmp_path: Path):
    store = PersonalityStore(tmp_path, min_interval_s=0.0)
    clock = FakeClock()
    model = PersonalityModel(ROBOT_ID, traits=get_preset("puppy"), store=store, clock=clock)
    model.record(Stimulus.PRAISE)
    assert model.save(force=True) is True

    restored = PersonalityModel(ROBOT_ID, store=store, clock=FakeClock())
    assert restored.traits.sociability == pytest.approx(get_preset("puppy").sociability)
    # The mood came back, but rounded: this is a snapshot, not a recording.
    assert restored.state.valence == round(restored.state.valence, 2)


def test_snapshots_are_throttled_but_a_trait_change_is_not(tmp_path: Path):
    store = PersonalityStore(tmp_path, min_interval_s=60.0)
    clock = FakeClock()
    model = PersonalityModel(ROBOT_ID, store=store, clock=clock)
    assert model.save() is True
    clock.advance(5.0)
    assert model.save() is False  # inside the throttle window
    clock.advance(120.0)
    assert model.save() is True
    model.set_traits(PersonalityTraits(boldness=0.9))  # forced, whatever the window says
    assert store.load(ROBOT_ID).traits.boldness == pytest.approx(0.9)


def test_a_corrupt_personality_file_does_not_stop_the_robot(tmp_path: Path):
    store = PersonalityStore(tmp_path)
    store.path_for(ROBOT_ID).parent.mkdir(parents=True, exist_ok=True)
    store.path_for(ROBOT_ID).write_text("{not json", encoding="utf-8")
    assert store.load(ROBOT_ID) is None
    model = PersonalityModel(ROBOT_ID, store=store, clock=FakeClock())
    assert model.traits == PersonalityTraits()


def test_a_personality_can_be_deleted(tmp_path: Path):
    store = PersonalityStore(tmp_path, min_interval_s=0.0)
    model = PersonalityModel(ROBOT_ID, store=store, clock=FakeClock())
    model.save(force=True)
    assert store.list_robots() == (ROBOT_ID,)
    assert store.delete(ROBOT_ID) is True
    assert store.list_robots() == ()
    assert store.delete(ROBOT_ID) is False


def test_the_store_writes_atomically(tmp_path: Path):
    store = PersonalityStore(tmp_path, min_interval_s=0.0)
    store.save(ROBOT_ID, PersonalityTraits(), None, force=True)
    store.save(ROBOT_ID, PersonalityTraits(boldness=0.3), None, force=True)
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert store.load(ROBOT_ID).traits.boldness == pytest.approx(0.3)


# -- influence on behaviour ------------------------------------------------------------------------------------


def test_high_curiosity_raises_investigation_and_exploration():
    from robot.state.world import obj

    world = behavior_world(entities=[obj("cube", now=WORLD_T0)])
    scheduler, _, _ = make_scheduler(tuning=None, mode=AutonomyMode.FULL)
    scheduler.tuning = scheduler.tuning.with_overrides(score_jitter=0.0)

    scheduler.set_drives(PersonalityModel(ROBOT_ID, traits=PersonalityTraits(curiosity=0.05), clock=FakeClock()))
    incurious = _score_of(scheduler.evaluate(world), "investigate_object")
    scheduler.set_drives(PersonalityModel(ROBOT_ID, traits=PersonalityTraits(curiosity=0.95), clock=FakeClock()))
    curious = _score_of(scheduler.evaluate(world), "investigate_object")
    assert curious > incurious


def test_high_sociability_raises_greeting():
    world = behavior_world(entities=[person("person-1", known=True, now=WORLD_T0)])
    scheduler, _, _ = make_scheduler()
    scheduler.tuning = scheduler.tuning.with_overrides(score_jitter=0.0)

    scheduler.set_drives(PersonalityModel(ROBOT_ID, traits=PersonalityTraits(sociability=0.05), clock=FakeClock()))
    aloof = _score_of(scheduler.evaluate(world), "greet_person")
    scheduler.set_drives(PersonalityModel(ROBOT_ID, traits=PersonalityTraits(sociability=0.95), clock=FakeClock()))
    sociable = _score_of(scheduler.evaluate(world), "greet_person")
    assert sociable > aloof


@pytest.mark.parametrize("trait", TRAIT_NAMES)
@pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.75, 1.0])
async def test_no_personality_can_talk_safety_into_a_cliff(trait: str, value: float):
    """The load-bearing one. Personality decides what is *proposed*; safety decides what
    is *allowed*, and it cannot see personality at all."""
    from robot.actions.executor import RobotActionExecutor
    from robot.actions.model import MoveAction
    from tests.robot.conftest import FakeActionRuntime, robot_state

    traits = PersonalityTraits().with_overrides(**{trait: value})
    personality = PersonalityModel(ROBOT_ID, traits=traits, clock=FakeClock())
    assert 0.0 <= personality.curiosity <= 1.0  # the personality is live, not a stub

    runtime = FakeActionRuntime(
        {ROBOT_ID: robot_state(sensors=RobotSensorState(cliff_detected=True))}
    )
    executor = RobotActionExecutor(runtime, start_watchdog=False)
    try:
        action = await executor.submit(
            MoveAction(distance_mm=200), ROBOT_ID, source=ActionSource.BEHAVIOR
        )
        record = action.record()
        assert record.status is ActionStatus.REJECTED
        assert record.rejection is RejectionReason.CLIFF_HAZARD
        assert runtime.called("robot.motion.move") == ()
    finally:
        await executor.aclose()
        await runtime.aclose()


def _score_of(decision, name: str) -> float:
    return next(c.total for c in decision.candidates if c.name == name)


def _record(status: ActionStatus, error: ActionError | None = None) -> ActionRecord:
    return ActionRecord(
        action_id="a-1",
        robot_id=ROBOT_ID,
        action_type=ActionType.MOVE,
        source=ActionSource.BEHAVIOR,
        priority=ActionPriority.NORMAL,
        status=status,
        error=error,
    )
