"""The sixteen built-in behaviours: when each wins, and what it commands when it does.

The scoring rules asserted here are the ones the design states in words — a flat battery
outranks a greeting, a person greeted a moment ago is not greeted again, boredom rises
with time — so a change that breaks one of them breaks a test rather than a demo.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from robot.behavior.base import AutonomyMode, NeutralDrives
from robot.behavior.builtins import (
    ASLEEP,
    BUILTIN_BEHAVIORS,
    GREETED,
    default_behaviors,
)
from robot.behavior.tuning import BehaviorTuning
from robot.state.world import Entity, EntityType, Environment, ImagePoint, Position, obj, person
from tests.robot.conftest import WORLD_T0, behavior_world, make_scheduler

NO_JITTER = BehaviorTuning(score_jitter=0.0)


def dock(distance_mm: int = 2000, bearing_deg: float = 0.0) -> Entity:
    return Entity(
        id="dock-1",
        type=EntityType.LOCATION,
        attributes={"label": "charger"},
        position=Position(distance_mm=distance_mm, bearing_deg=bearing_deg),
        first_seen=WORLD_T0,
        last_seen=WORLD_T0,
        updated_at=WORLD_T0,
    )


def visitor(entity_id: str = "person-1", **kwargs) -> Entity:
    kwargs.setdefault("now", WORLD_T0)
    return person(entity_id, **kwargs)


async def run_once(scheduler, world):
    """Tick, then let the winning behaviour run to completion."""
    import asyncio

    decision = await scheduler.tick(world)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return decision


# -- the set ------------------------------------------------------------------------------------


def test_every_behaviour_the_design_names_is_registered():
    names = {behavior.name for behavior in default_behaviors()}
    assert names == {
        "idle",
        "look_around",
        "explore",
        "greet_person",
        "look_at_person",
        "approach_person",
        "follow_person",
        "react_to_touch",
        "react_to_sound",
        "investigate_object",
        "bored",
        "low_battery",
        "go_to_charger",
        "charging",
        "wake",
        "sleep",
    }
    assert len(names) == len(BUILTIN_BEHAVIORS)


def test_a_behaviour_can_be_swapped_out_by_name():
    class Loud(type(default_behaviors()[-1])):  # subclass of IdleBehavior
        name = "idle"

    behaviors = default_behaviors(idle=Loud())
    assert isinstance(next(b for b in behaviors if b.name == "idle"), Loud)
    with pytest.raises(ValueError, match="no built-in behaviour"):
        default_behaviors(nonsense=None)


# -- power --------------------------------------------------------------------------------------------


def test_a_critical_battery_beats_a_person_in_the_room():
    """`GoToCharger: battery < 10% = extremely high` — and it is a band above social."""
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.FULL)
    world = behavior_world(battery_percent=8, entities=[dock(), visitor(known=True)])
    decision = scheduler.evaluate(world)
    assert decision.selected == "go_to_charger"
    assert decision.score >= 0.95
    assert dict(
        (c.name, c.total) for c in decision.candidates if c.eligible
    )["greet_person"] < decision.score


def test_docking_urgency_rises_as_the_battery_falls():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    scores = []
    for percent in (20, 15, 10, 5):
        world = behavior_world(battery_percent=percent, entities=[dock()])
        decision = scheduler.evaluate(world)
        assert decision.selected == "go_to_charger"
        scores.append(decision.score)
    assert scores == sorted(scores)
    assert scores[0] < scores[-1]


def test_a_healthy_battery_does_not_dock():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world(battery_percent=90, entities=[dock()]))
    assert decision.selected != "go_to_charger"


def test_without_a_known_dock_the_robot_says_it_is_low_instead():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world(battery_percent=9))
    assert decision.selected == "low_battery"


async def test_docking_turns_towards_the_dock_and_drives():
    scheduler, robot, _ = make_scheduler(tuning=NO_JITTER)
    await run_once(scheduler, behavior_world(battery_percent=8, entities=[dock(bearing_deg=-30)]))
    assert robot.commands == ("turn", "move")
    assert robot.arguments("turn")[0]["angle_deg"] == -30
    await scheduler.aclose()


def test_a_charging_robot_stays_on_the_dock_until_it_is_charged_enough():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    assert scheduler.evaluate(behavior_world(battery_percent=50, charging=True)).selected == "charging"
    assert scheduler.evaluate(behavior_world(battery_percent=99, charging=True)).selected != "charging"


# -- social ---------------------------------------------------------------------------------------------------


def test_a_newly_detected_familiar_person_is_greeted():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world(entities=[visitor(known=True, name="Ahmad")]))
    assert decision.selected == "greet_person"
    assert "person person-1 newly detected" in decision.reasons
    assert "familiar person" in decision.reasons
    assert "greet cooldown expired" in decision.reasons


def test_a_familiar_person_scores_above_a_stranger():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    known = scheduler.evaluate(behavior_world(entities=[visitor(known=True)])).score
    stranger = scheduler.evaluate(behavior_world(entities=[visitor(known=False)])).score
    assert known > stranger


async def test_a_person_greeted_a_moment_ago_is_not_greeted_again():
    """`GreetPerson: recently greeted = near zero` — expressed as ineligibility."""
    scheduler, robot, clock = make_scheduler(tuning=NO_JITTER)
    world = behavior_world(entities=[visitor(known=True)])
    assert (await run_once(scheduler, world)).selected == "greet_person"
    assert "excited_greeting" in [a["name"] for a in robot.arguments("play_animation")]

    clock.advance(5.0)
    decision = scheduler.evaluate(world)
    assert decision.selected != "greet_person"
    assert not any(c.name == "greet_person" and c.eligible for c in decision.candidates)

    clock.advance(BehaviorTuning().greet_cooldown_s + 1)
    assert scheduler.evaluate(world).selected == "greet_person"
    await scheduler.aclose()


def test_a_person_who_has_not_been_seen_for_a_while_is_not_present():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    stale = WORLD_T0 - timedelta(seconds=30)
    world = behavior_world(entities=[person("person-1", now=stale)])
    decision = scheduler.evaluate(world)
    assert decision.selected != "greet_person"


def test_looking_at_a_person_needs_them_to_be_off_centre():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    centred = behavior_world(
        entities=[visitor(image_point=ImagePoint(x=0.5, y=0.5, width=0.2, height=0.4))]
    )
    names = {c.name for c in scheduler.evaluate(centred).candidates if c.eligible}
    assert "look_at_person" not in names

    off = behavior_world(
        entities=[visitor(image_point=ImagePoint(x=0.15, y=0.5, width=0.2, height=0.4))]
    )
    assert "look_at_person" in {c.name for c in scheduler.evaluate(off).candidates if c.eligible}


async def test_looking_at_a_person_commands_the_normalized_point_as_percentages():
    greeted = make_scheduler(tuning=NO_JITTER)
    scheduler, robot, clock = greeted
    world = behavior_world(
        entities=[visitor(known=True, image_point=ImagePoint(x=0.2, y=0.8, width=0.2, height=0.3))]
    )
    await run_once(scheduler, world)  # greeting wins first
    clock.advance(1.0)
    await run_once(scheduler, world)
    assert robot.arguments("look_at")[0] == {"x_pct": 20, "y_pct": 80}
    await scheduler.aclose()


async def test_following_hands_the_target_to_the_device_loop():
    scheduler, robot, clock = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.FULL)
    world = behavior_world(
        entities=[visitor(known=True, position=Position(distance_mm=1500, bearing_deg=0.0))]
    ).attend_to("person-1", now=WORLD_T0)
    await run_once(scheduler, world)  # greeting
    clock.advance(1.0)
    decision = await run_once(scheduler, world)
    assert decision.selected in {"follow_person", "approach_person"}
    if decision.selected == "follow_person":
        assert robot.arguments("follow")[0]["target_id"] == "person-1"
    await scheduler.aclose()


def test_approaching_needs_the_person_to_be_far_enough_away():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.FULL)
    close = behavior_world(entities=[visitor(position=Position(distance_mm=500))])
    assert "approach_person" not in {c.name for c in scheduler.evaluate(close).candidates if c.eligible}
    far = behavior_world(entities=[visitor(position=Position(distance_mm=2500))])
    assert "approach_person" in {c.name for c in scheduler.evaluate(far).candidates if c.eligible}


def test_approaching_is_refused_while_a_sensor_says_the_way_is_blocked():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.FULL)
    world = behavior_world(cliff=True, entities=[visitor(position=Position(distance_mm=2500))])
    eligible = {c.name for c in scheduler.evaluate(world).candidates if c.eligible}
    assert "approach_person" not in eligible
    assert "explore" not in eligible


# -- reactions -----------------------------------------------------------------------------------------------------


async def test_a_touch_outranks_everything_social():
    scheduler, robot, _ = make_scheduler(tuning=NO_JITTER)
    world = behavior_world(touch=True, entities=[visitor(known=True)])
    decision = await run_once(scheduler, world)
    assert decision.selected == "react_to_touch"
    assert robot.arguments("set_expression")[0]["emotion"] == "happy"
    await scheduler.aclose()


def test_a_quiet_room_is_not_a_sound_to_react_to():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    quiet = behavior_world().with_environment(
        Environment(sound_level=0.1, updated_at=WORLD_T0), now=WORLD_T0
    )
    assert "react_to_sound" not in {c.name for c in scheduler.evaluate(quiet).candidates if c.eligible}


async def test_a_loud_noise_turns_the_head_towards_it():
    scheduler, robot, _ = make_scheduler(tuning=NO_JITTER)
    loud = behavior_world().with_environment(
        Environment(sound_level=0.9, sound_direction_deg=-40.0, updated_at=WORLD_T0), now=WORLD_T0
    )
    decision = await run_once(scheduler, loud)
    assert decision.selected == "react_to_sound"
    assert robot.arguments("head_angle")[0]["yaw_deg"] == -40
    await scheduler.aclose()


def test_an_old_sound_is_not_a_stimulus():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    stale = behavior_world().with_environment(
        Environment(sound_level=0.9, updated_at=WORLD_T0 - timedelta(seconds=30)), now=WORLD_T0
    )
    assert "react_to_sound" not in {c.name for c in scheduler.evaluate(stale).candidates if c.eligible}


# -- curiosity ---------------------------------------------------------------------------------------------------------


def test_a_new_object_is_worth_investigating_and_curiosity_raises_the_score():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    world = behavior_world(entities=[obj("cube", label="cube", now=WORLD_T0)])
    assert scheduler.evaluate(world).selected == "investigate_object"
    baseline = scheduler.evaluate(world).score

    scheduler.set_drives(NeutralDrives(curiosity=1.0))
    assert scheduler.evaluate(world).score > baseline


async def test_an_object_is_not_investigated_twice_in_a_row():
    scheduler, _, clock = make_scheduler(tuning=NO_JITTER)
    world = behavior_world(entities=[obj("cube", now=WORLD_T0)])
    assert (await run_once(scheduler, world)).selected == "investigate_object"
    clock.advance(1.0)
    assert (await run_once(scheduler, world)).selected != "investigate_object"
    await scheduler.aclose()


def test_exploring_needs_an_empty_room():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.FULL)
    empty = {c.name for c in scheduler.evaluate(behavior_world()).candidates if c.eligible}
    assert "explore" in empty
    busy = {
        c.name
        for c in scheduler.evaluate(behavior_world(entities=[visitor()])).candidates
        if c.eligible
    }
    assert "explore" not in busy


def test_boredom_rises_with_time_and_is_zero_before_the_onset():
    tuning = BehaviorTuning(score_jitter=0.0)
    scheduler, _, _ = make_scheduler(tuning=tuning)

    fresh = behavior_world(interaction_ended_s=5.0)
    assert "bored" not in {c.name for c in scheduler.evaluate(fresh).candidates if c.eligible}

    scores = []
    for idle in (tuning.boredom_onset_s + 1, 120.0, tuning.boredom_full_s):
        decision = scheduler.evaluate(behavior_world(interaction_ended_s=idle))
        candidate = next(c for c in decision.candidates if c.name == "bored")
        assert candidate.eligible
        scores.append(candidate.score)
    assert scores == sorted(scores)
    assert scores[-1] == pytest.approx(tuning.boredom_max_score)


def test_an_open_interaction_means_the_robot_is_not_idle():
    from robot.state.world import Interaction

    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    talking = behavior_world().start_interaction(
        Interaction(id="chat", started_at=WORLD_T0), now=WORLD_T0
    )
    assert "bored" not in {c.name for c in scheduler.evaluate(talking).candidates if c.eligible}


def test_idle_is_the_floor_and_is_always_available():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    decision = scheduler.evaluate(behavior_world())
    assert "idle" in {c.name for c in decision.candidates if c.eligible}
    assert decision.selected != "idle"  # something is always better than nothing


def test_with_nothing_at_all_to_do_the_robot_still_selects_something():
    """The engine must never return "no idea": idle is the floor, not an empty answer."""
    tuning = BehaviorTuning(score_jitter=0.0, look_around_base_score=0.0, curiosity_weight=0.0)
    scheduler, _, _ = make_scheduler(tuning=tuning, mode=AutonomyMode.PASSIVE)
    assert scheduler.evaluate(behavior_world()).selected == "idle"


# -- sleep and wake -------------------------------------------------------------------------------------------------------


async def test_the_robot_sleeps_after_a_long_silence_and_wakes_on_a_stimulus():
    tuning = BehaviorTuning(score_jitter=0.0)
    scheduler, _, clock = make_scheduler(tuning=tuning)
    quiet = behavior_world(interaction_ended_s=tuning.sleep_after_idle_s + 10)
    assert (await run_once(scheduler, quiet)).selected == "sleep"
    assert scheduler.memory.at(ASLEEP) is not None

    clock.advance(5.0)
    scheduler.note_stimulus("wake_word")
    assert (await run_once(scheduler, quiet)).selected == "wake"
    assert scheduler.memory.at(ASLEEP) is None
    await scheduler.aclose()


async def test_a_person_walking_in_wakes_the_robot():
    tuning = BehaviorTuning(score_jitter=0.0)
    scheduler, _, clock = make_scheduler(tuning=tuning)
    await run_once(scheduler, behavior_world(interaction_ended_s=tuning.sleep_after_idle_s + 10))
    clock.advance(1.0)
    decision = await run_once(scheduler, behavior_world(entities=[visitor(known=True)]))
    assert decision.selected == "wake"
    await scheduler.aclose()


# -- memory keys ------------------------------------------------------------------------------------------------------------


async def test_greeting_records_the_person_not_the_behaviour():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER)
    await run_once(scheduler, behavior_world(entities=[visitor("person-1", known=True)]))
    assert scheduler.memory.at(GREETED + "person-1") is not None
    # A different person has not been greeted, so they still can be.
    decision = scheduler.evaluate(
        behavior_world(entities=[visitor("person-1", known=True), visitor("person-2")])
    )
    assert decision.selected == "greet_person"
    await scheduler.aclose()
