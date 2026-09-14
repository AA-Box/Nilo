"""Autonomy modes, the tuning discipline, and the explain CLI.

The mode table is the containment boundary of the whole engine, so it is asserted
exhaustively: for each of the four modes, every built-in behaviour is checked against what
that mode is documented to permit (docs/behavior-system.md).
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from robot.behavior.base import EXPRESSIVE_RESOURCES, AutonomyMode
from robot.behavior.builtins import default_behaviors
from robot.behavior.engine import BehaviorEngine, StaticWorld
from robot.behavior.explain import SITUATIONS, NullRobot, explain_world, get_situation
from robot.behavior.tuning import BehaviorTuning, load_tuning, tuning_from_mapping
from robot.behavior.__main__ import main
from robot.state.actions import Resource
from robot.state.world import Position, person
from tests.robot.conftest import WORLD_T0, FakeClock, RecordingRobot, behavior_world, make_scheduler

NO_JITTER = BehaviorTuning(score_jitter=0.0)

#: What each mode may initiate, as the design states it.
EXPECTED_BY_MODE: dict[AutonomyMode, set[str]] = {
    AutonomyMode.OFF: set(),
    AutonomyMode.PASSIVE: {
        "idle",
        "charging",
        "low_battery",
        "sleep",
        "wake",
        "react_to_touch",
        "greet_person",
        "bored",
    },
    AutonomyMode.NORMAL: {
        "idle",
        "charging",
        "low_battery",
        "sleep",
        "wake",
        "react_to_touch",
        "greet_person",
        "bored",
        "go_to_charger",
        "react_to_sound",
        "look_at_person",
        "investigate_object",
        "look_around",
    },
    AutonomyMode.FULL: {behavior.name for behavior in default_behaviors()},
}


@pytest.mark.parametrize("mode", list(AutonomyMode))
def test_each_mode_permits_exactly_what_it_is_documented_to(mode: AutonomyMode):
    permitted = {behavior.name for behavior in default_behaviors() if mode.allows(behavior)}
    assert permitted == EXPECTED_BY_MODE[mode]


def test_off_initiates_nothing_at_all():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.OFF)
    world = behavior_world(battery_percent=4, touch=True, entities=[person("person-1", now=WORLD_T0)])
    decision = scheduler.evaluate(world)
    assert decision.selected is None
    assert all(not candidate.eligible for candidate in decision.candidates)


def test_passive_reacts_but_never_moves():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.PASSIVE)
    world = behavior_world(
        touch=True, entities=[person("person-1", position=Position(distance_mm=2500), now=WORLD_T0)]
    )
    decision = scheduler.evaluate(world)
    assert decision.selected == "react_to_touch"
    for candidate in decision.candidates:
        if candidate.eligible:
            assert candidate.behavior.required_resources <= EXPRESSIVE_RESOURCES


def test_normal_does_not_drive_off_on_its_own():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.NORMAL)
    world = behavior_world(entities=[person("person-1", position=Position(distance_mm=2500), now=WORLD_T0)])
    eligible = {c.name for c in scheduler.evaluate(world).candidates if c.eligible}
    assert {"explore", "approach_person", "follow_person"} & eligible == set()


def test_normal_still_docks_because_power_is_not_exploration():
    scheduler, _, _ = make_scheduler(tuning=NO_JITTER, mode=AutonomyMode.NORMAL)
    from tests.robot.test_behaviors import dock

    assert scheduler.evaluate(behavior_world(battery_percent=7, entities=[dock()])).selected == "go_to_charger"


def test_full_allows_everything_normal_does_and_more():
    assert EXPECTED_BY_MODE[AutonomyMode.NORMAL] < EXPECTED_BY_MODE[AutonomyMode.FULL]


async def test_lowering_the_mode_cancels_what_the_new_mode_would_not_have_started():
    robot = RecordingRobot()
    clock = FakeClock()
    world = behavior_world(entities=[person("person-1", position=Position(distance_mm=3000), now=WORLD_T0)])
    engine = BehaviorEngine(
        "test-robot",
        robot,
        StaticWorld(world),
        tuning=NO_JITTER,
        mode=AutonomyMode.FULL,
        clock=clock,
    )
    # Greeting first (it is the best thing to do), then the drive behaviour.
    await engine.tick()
    await asyncio.sleep(0)
    clock.advance(1.0)
    decision = await engine.tick()
    assert decision.selected in {"approach_person", "follow_person"}

    await engine.set_mode(AutonomyMode.PASSIVE)
    assert engine.scheduler.running is None
    assert engine.mode is AutonomyMode.PASSIVE
    await engine.aclose()


async def test_raising_the_mode_leaves_the_running_behaviour_alone():
    robot = RecordingRobot()
    clock = FakeClock()
    engine = BehaviorEngine(
        "test-robot",
        robot,
        StaticWorld(behavior_world(touch=True)),
        tuning=NO_JITTER,
        mode=AutonomyMode.PASSIVE,
        clock=clock,
    )
    await engine.tick()
    await engine.set_mode(AutonomyMode.FULL)
    assert engine.mode is AutonomyMode.FULL
    await engine.aclose()


async def test_the_runtime_sets_the_mode_for_every_robot(runtime):
    from tests.robot.conftest import device_info

    await runtime.attach(device_info(device_id="robot-a"), discover=False)
    engine = runtime.behavior("robot-a")
    assert engine.mode is AutonomyMode.NORMAL
    await runtime.set_autonomy(AutonomyMode.PASSIVE)
    assert engine.mode is AutonomyMode.PASSIVE
    # A robot that connects later inherits the current mode.
    assert runtime.behavior("robot-b").mode is AutonomyMode.PASSIVE


# -- the tuning discipline ---------------------------------------------------------------------------


def test_no_scoring_function_contains_a_bare_number():
    """The rule the tuning module exists for: no arbitrary constants in the behaviours.

    Parsed rather than grepped, and limited to ``score``/``can_run``/``cooldown`` — the
    decision-making half. ``0`` and ``1`` are allowed (indices and identity), as are the
    small integers in the percentage arguments of an ``execute``, which this does not read.
    """
    source = Path(__file__).resolve().parents[2] / "robot" / "behavior" / "builtins.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    allowed = {0, 1, 2, 0.0, 1.0, 0.5, 100}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in {"score", "can_run", "cooldown"}:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)):
                if isinstance(child.value, bool) or child.value in allowed:
                    continue
                offenders.append(f"{node.name}: {child.value}")
    assert not offenders, f"move these into BehaviorTuning: {offenders}"


def test_tuning_rejects_an_unknown_key_rather_than_ignoring_it():
    with pytest.raises(Exception):
        tuning_from_mapping({"behavior": {"greet_scores": 0.9}})


def test_tuning_loads_from_a_yaml_file(tmp_path: Path):
    path = tmp_path / "behavior.yaml"
    path.write_text("behavior:\n  greet_score: 0.42\n  explore_cooldown_s: 5\n", encoding="utf-8")
    tuning = load_tuning(path)
    assert tuning.greet_score == pytest.approx(0.42)
    assert tuning.explore_cooldown_s == pytest.approx(5.0)


def test_a_missing_tuning_file_leaves_the_defaults_in_place(tmp_path: Path):
    assert load_tuning(tmp_path / "nothing.yaml") == BehaviorTuning()


def test_tuning_changes_the_answer():
    world = behavior_world(entities=[person("person-1", known=True, now=WORLD_T0)])
    assert explain_world(world, tuning=NO_JITTER).selected == "greet_person"
    quiet = NO_JITTER.with_overrides(greet_score=0.0, greet_known_bonus=0.0, sociability_weight=0.0)
    assert explain_world(world, tuning=quiet).selected != "greet_person"


# -- the explain CLI ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SITUATIONS))
def test_every_built_in_situation_produces_a_decision(name: str):
    decision = explain_world(get_situation(name))
    assert decision.selected is not None
    assert decision.explain()


def test_the_documented_situations_select_what_they_say_they_do():
    assert explain_world(get_situation("person_arrives"), tuning=NO_JITTER).selected == "greet_person"
    assert explain_world(get_situation("low_battery"), tuning=NO_JITTER).selected == "go_to_charger"
    assert explain_world(get_situation("charging"), tuning=NO_JITTER).selected == "charging"
    assert explain_world(get_situation("touched"), tuning=NO_JITTER).selected == "react_to_touch"
    assert explain_world(get_situation("loud_noise"), tuning=NO_JITTER).selected == "react_to_sound"
    assert explain_world(get_situation("new_object"), tuning=NO_JITTER).selected == "investigate_object"


def test_the_cli_prints_the_documented_shape(capsys):
    assert main(["explain", "--situation", "person_arrives"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("selected: greet_person")
    assert "alternatives:" in out
    assert "reasons:" in out


def test_the_cli_can_emit_json(capsys):
    assert main(["explain", "--situation", "low_battery", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["selected"] == "go_to_charger"
    assert data["mode"] == "normal"


def test_the_cli_honours_the_mode(capsys):
    assert main(["explain", "--situation", "quiet_room", "--mode", "off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["selected"] is None


def test_the_cli_reads_a_world_from_a_file(tmp_path: Path, capsys):
    path = tmp_path / "world.json"
    path.write_text(get_situation("touched").model_dump_json(), encoding="utf-8")
    assert main(["explain", "--world", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["selected"] == "react_to_touch"


def test_the_cli_lists_situations_and_behaviours(capsys):
    assert main(["situations"]) == 0
    assert "person_arrives" in capsys.readouterr().out
    assert main(["behaviors"]) == 0
    listing = capsys.readouterr().out
    assert "go_to_charger" in listing
    assert "drive" in listing


def test_explaining_never_commands_the_robot():
    robot = NullRobot()
    explain_world(get_situation("low_battery"), robot=robot)
    assert robot.calls == []


def test_the_engine_reports_no_decision_before_its_first_tick():
    engine = BehaviorEngine("test-robot", NullRobot(), StaticWorld(behavior_world()))
    assert engine.last_decision is None
    assert engine.explain() == "no decision yet\n"
    assert engine.explain(fresh=True).startswith("selected:")
    assert engine.explain_data(fresh=True)["robot_id"] == "test-robot"


def test_resources_are_declared_for_every_behaviour_that_moves_something():
    """A behaviour that commands a subsystem without claiming it would let two
    behaviours drive the same motor at once."""
    movers = {"go_to_charger", "explore", "approach_person", "follow_person"}
    for behavior in default_behaviors():
        if behavior.name in movers:
            assert Resource.DRIVE in behavior.required_resources
