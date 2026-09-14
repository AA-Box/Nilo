"""Animations: the data, the loader, and the engine that plays them.

Every test here runs on a fake clock and a fake sleeper, so a four-second animation plays
in no time at all and the exact command order can be asserted. Timing that is data can be
tested; timing that is `await asyncio.sleep(0.4)` cannot.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from robot.animation.engine import AnimationEngine, AnimationRefused, HandlePlayer
from robot.animation.library import (
    BUILTIN_LIBRARY_DIR,
    EXPRESSIONS,
    AnimationError,
    AnimationLibrary,
    load_directory,
    load_file,
    load_library,
    parse_document,
)
from robot.animation.model import CHANNEL_RESOURCES, Animation, AnimationStep, Channel
from robot.events.bus import EventBus
from robot.events.types import AnimationCancelled, AnimationFinished, AnimationStarted
from robot.state.actions import Resource
from tests.robot.conftest import ROBOT_ID, FakeClock

WAVE = {
    "name": "wave",
    "priority": 50,
    "transition": "neutral",
    "steps": [
        {"at_ms": 0, "channel": "eyes", "action": "expression", "args": {"emotion": "happy"}},
        {"at_ms": 200, "channel": "head", "action": "angle", "args": {"yaw_deg": 20}},
        {"at_ms": 400, "channel": "head", "action": "angle", "args": {"yaw_deg": -20}},
    ],
}


class FakePlayer:
    """Records the commands an animation issues, in order, and can be made to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_on: set[str] = set()

    def _record(self, command: str, **kwargs) -> str:
        self.calls.append((command, kwargs))
        if command in self.fail_on:
            raise RuntimeError(f"{command} failed")
        return "ok"

    async def set_expression(self, emotion: str, intensity_pct: int = 100, **kwargs):
        return self._record("set_expression", emotion=emotion, intensity_pct=intensity_pct)

    async def head_angle(self, pitch_deg: int = 0, yaw_deg: int = 0, **kwargs):
        return self._record("head_angle", pitch_deg=pitch_deg, yaw_deg=yaw_deg)

    async def look_at(self, x_pct: int = 50, y_pct: int = 50, **kwargs):
        return self._record("look_at", x_pct=x_pct, y_pct=y_pct)

    async def lift(self, height_pct: int, **kwargs):
        return self._record("lift", height_pct=height_pct)

    async def turn(self, angle_deg: int, **kwargs):
        return self._record("turn", angle_deg=angle_deg)

    async def move(self, distance_mm: int, **kwargs):
        return self._record("move", distance_mm=distance_mm)

    async def play_sound(self, sound: str, **kwargs):
        return self._record("play_sound", sound=sound)

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.calls)


class FakeSleep:
    """A sleeper that advances a fake clock instead of waiting, and records what it was asked for."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(round(seconds, 4))
        self.clock.advance(seconds)
        await asyncio.sleep(0)


def build_engine(
    *animations: Animation,
    events: EventBus | None = None,
    energy: float | None = None,
) -> tuple[AnimationEngine, FakePlayer, FakeClock, FakeSleep]:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    player = FakePlayer()
    library = AnimationLibrary(animations or [Animation.model_validate(WAVE)])
    engine = AnimationEngine(
        ROBOT_ID,
        player,
        library,
        events=events,
        clock=clock,
        sleep=sleep,
        energy=(lambda: energy) if energy is not None else None,
    )
    return engine, player, clock, sleep


# -- the data -----------------------------------------------------------------------------------


def test_the_shipped_library_loads_and_covers_what_the_behaviours_ask_for():
    library = load_library()
    assert len(library) >= 12
    for name in ("excited_greeting", "happy_wiggle", "bored_sigh", "idle_breathe"):
        assert name in library, name


def test_every_shipped_animation_uses_a_known_expression():
    for animation in load_library():
        for step in animation.steps:
            if step.channel is Channel.EYES and step.action == "expression":
                assert step.args["emotion"] in EXPRESSIONS, animation.name
        if animation.transition is not None:
            assert animation.transition in EXPRESSIONS, animation.name


def test_channels_map_onto_the_resource_ledger():
    assert CHANNEL_RESOURCES[Channel.EYES] is Resource.DISPLAY
    assert CHANNEL_RESOURCES[Channel.BODY] is Resource.DRIVE
    animation = Animation.model_validate(WAVE)
    assert animation.required_resources == frozenset({Resource.DISPLAY, Resource.HEAD})


def test_steps_are_sorted_by_offset_however_they_were_written():
    animation = Animation.model_validate(
        {
            "name": "out_of_order",
            "steps": [
                {"at_ms": 500, "channel": "head", "action": "angle"},
                {"at_ms": 100, "channel": "eyes", "action": "expression"},
            ],
        }
    )
    assert [step.at_ms for step in animation.steps] == [100, 500]


def test_an_unknown_action_on_a_channel_is_refused_at_load():
    with pytest.raises(Exception, match="has no action"):
        AnimationStep(channel=Channel.LIFT, action="pirouette")


def test_a_bad_animation_names_its_file():
    with pytest.raises(AnimationError, match="nonsense.yaml"):
        parse_document({"name": "bad", "steps": [{"channel": "head", "action": "explode"}]}, source="nonsense.yaml")


def test_two_animations_with_one_name_is_an_error():
    library = AnimationLibrary([Animation(name="a")])
    with pytest.raises(AnimationError, match="two animations are named"):
        library.add(Animation(name="a"))
    library.add(Animation(name="a", description="deliberate"), replace=True)
    assert library.require("a").description == "deliberate"


def test_a_deployment_directory_can_replace_a_shipped_animation(tmp_path: Path):
    (tmp_path / "override.yaml").write_text(
        "animations:\n  excited_greeting:\n    description: quieter\n    steps:\n"
        "      - {at_ms: 0, channel: eyes, action: expression, args: {emotion: happy}}\n",
        encoding="utf-8",
    )
    library = load_library(tmp_path)
    assert library.require("excited_greeting").description == "quieter"
    assert "idle_breathe" in library  # the rest of the shipped set is still there


def test_a_new_animation_needs_no_python(tmp_path: Path):
    """The acceptance criterion, as a test: a file is the whole change."""
    (tmp_path / "new.yaml").write_text(
        "name: victory_spin\n"
        "transition: happy\n"
        "steps:\n"
        "  - {at_ms: 0, channel: eyes, action: expression, args: {emotion: excited}}\n"
        "  - {at_ms: 100, channel: body, action: turn, args: {angle_deg: 45}}\n",
        encoding="utf-8",
    )
    library = load_directory(tmp_path)
    animation = library.require("victory_spin")
    assert animation.length_ms == 100
    assert Resource.DRIVE in animation.required_resources


def test_a_json_library_loads_too(tmp_path: Path):
    (tmp_path / "a.json").write_text('{"name": "blink", "steps": []}', encoding="utf-8")
    assert "blink" in load_directory(tmp_path)


def test_tags_group_animations():
    library = load_library()
    assert {a.name for a in library.tagged("greeting")} >= {"excited_greeting", "calm_greeting"}


def test_the_shipped_files_are_the_only_source_of_shipped_animations():
    """No animation is constructed in Python: the library directory is the whole set."""
    from_files: set[str] = set()
    for path in BUILTIN_LIBRARY_DIR.iterdir():
        if path.suffix in {".yaml", ".yml"}:
            from_files |= {animation.name for animation in load_file(path)}
    assert from_files == set(load_library(builtins=True).names())


# -- playing --------------------------------------------------------------------------------------------


async def test_steps_play_in_order_on_their_declared_offsets():
    engine, player, _, sleep = build_engine()
    playback = await engine.play("wave")
    assert playback is not None
    await engine.wait_for("wave")
    assert player.commands == ("set_expression", "head_angle", "head_angle", "set_expression")
    # 0 ms, then 200 ms, then another 200 ms. Offsets, not cumulative delays.
    assert sleep.waits[:2] == [0.2, 0.2]


async def test_the_transition_leaves_the_face_where_the_animation_said():
    engine, player, _, _ = build_engine()
    await engine.play("wave")
    await engine.wait_for("wave")
    assert player.calls[-1] == ("set_expression", {"emotion": "neutral", "intensity_pct": 100})


async def test_a_cancelled_animation_still_runs_its_transition():
    looping = Animation.model_validate({**WAVE, "loop": True})
    engine, player, _, _ = build_engine(looping)
    await engine.play("wave")
    await asyncio.sleep(0)
    await engine.cancel("wave")
    assert player.calls[-1][0] == "set_expression"
    assert player.calls[-1][1]["emotion"] == "neutral"
    assert engine.playing == ()


async def test_a_looping_animation_repeats_until_cancelled():
    looping = Animation.model_validate({**WAVE, "loop": True})
    engine, player, _, _ = build_engine(looping)
    playback = await engine.play("wave")
    assert playback is not None
    for _ in range(20):
        await asyncio.sleep(0)
    assert playback.passes >= 2
    await engine.cancel("wave")


async def test_a_step_that_fails_does_not_stop_the_animation():
    engine, player, _, _ = build_engine()
    player.fail_on = {"head_angle"}
    await engine.play("wave")
    await engine.wait_for("wave")
    assert player.commands.count("head_angle") == 2  # both were attempted
    assert engine.playing == ()


# -- ownership and priority ---------------------------------------------------------------------------------


async def test_an_animation_owns_its_channels_while_it_plays():
    looping = Animation.model_validate({**WAVE, "loop": True})
    engine, _, _, _ = build_engine(looping)
    await engine.play("wave")
    assert engine.owner_of(Resource.HEAD) == "wave"
    assert engine.owner_of(Resource.DRIVE) is None
    await engine.cancel("wave")
    assert engine.owner_of(Resource.HEAD) is None


async def test_a_lower_priority_animation_is_refused_while_the_head_is_held():
    holder = Animation.model_validate({**WAVE, "name": "holder", "priority": 80, "loop": True})
    quiet = Animation.model_validate({**WAVE, "name": "quiet", "priority": 20})
    engine, player, _, _ = build_engine(holder, quiet)
    await engine.play("holder")
    assert await engine.play("quiet") is None
    assert engine.playing == ("holder",)
    with pytest.raises(AnimationRefused):
        await engine.require("quiet")
    await engine.cancel()


async def test_a_higher_priority_animation_preempts_and_takes_the_resources():
    holder = Animation.model_validate({**WAVE, "name": "holder", "priority": 20, "loop": True})
    urgent = Animation.model_validate({**WAVE, "name": "urgent", "priority": 90})
    engine, _, _, _ = build_engine(holder, urgent)
    await engine.play("holder")
    assert await engine.play("urgent") is not None
    assert engine.owner_of(Resource.HEAD) == "urgent"
    assert "holder" not in engine.playing
    await engine.cancel()


async def test_animations_that_share_no_resources_play_together():
    face = Animation.model_validate(
        {
            "name": "face",
            "loop": True,
            "steps": [{"at_ms": 0, "channel": "eyes", "action": "expression", "args": {"emotion": "happy"}}],
        }
    )
    legs = Animation.model_validate(
        {
            "name": "legs",
            "loop": True,
            "steps": [{"at_ms": 0, "channel": "body", "action": "turn", "args": {"angle_deg": 10}}],
        }
    )
    engine, _, _, _ = build_engine(face, legs)
    await engine.play("face")
    await engine.play("legs")
    assert engine.playing == ("face", "legs")
    await engine.cancel()


async def test_playing_the_same_animation_again_restarts_it():
    looping = Animation.model_validate({**WAVE, "loop": True})
    engine, _, _, _ = build_engine(looping)
    first = await engine.play("wave")
    second = await engine.play("wave")
    assert first is not second
    assert engine.playing == ("wave",)
    await engine.cancel()


# -- the energy gate ------------------------------------------------------------------------------------------


async def test_an_energetic_animation_is_suppressed_when_the_robot_is_tired():
    energetic = Animation.model_validate({**WAVE, "name": "big", "energetic": True})
    calm = Animation.model_validate({**WAVE, "name": "small", "energetic": False})
    engine, player, _, _ = build_engine(energetic, calm, energy=0.05)
    assert await engine.play("big") is None
    assert player.calls == []
    assert await engine.play("small") is not None
    await engine.cancel()


async def test_the_same_animation_plays_when_the_robot_is_rested():
    energetic = Animation.model_validate({**WAVE, "name": "big", "energetic": True})
    engine, _, _, _ = build_engine(energetic, energy=0.9)
    assert await engine.play("big") is not None
    await engine.cancel()


async def test_a_missing_animation_is_a_refusal_not_a_crash():
    engine, player, _, _ = build_engine()
    assert await engine.play("nonexistent") is None
    assert player.calls == []


# -- events -------------------------------------------------------------------------------------------------------


async def test_playing_is_published():
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe((AnimationStarted, AnimationFinished, AnimationCancelled), seen.append)
    engine, _, _, _ = build_engine(events=bus)
    await engine.play("wave")
    await engine.wait_for("wave")
    await bus.drain()

    started = next(e for e in seen if isinstance(e, AnimationStarted))
    assert started.animation == "wave"
    assert set(started.resources) == {"display", "head"}
    finished = next(e for e in seen if isinstance(e, AnimationFinished))
    assert finished.outcome == "completed"
    await bus.aclose()


async def test_a_refusal_is_published_with_its_reason():
    bus = EventBus()
    refusals: list[AnimationCancelled] = []
    bus.subscribe(AnimationCancelled, refusals.append)
    energetic = Animation.model_validate({**WAVE, "name": "big", "energetic": True})
    engine, _, _, _ = build_engine(energetic, events=bus, energy=0.0)
    await engine.play("big")
    await bus.drain()
    assert [e.reason for e in refusals] == ["low_energy"]
    await bus.aclose()


# -- determinism ---------------------------------------------------------------------------------------------------------


async def test_the_same_animation_produces_the_same_commands_every_time():
    runs = []
    for _ in range(25):
        engine, player, _, sleep = build_engine()
        await engine.play("wave")
        await engine.wait_for("wave")
        commands = tuple((name, tuple(sorted(args.items()))) for name, args in player.calls)
        runs.append((commands, tuple(sleep.waits)))
    assert len(set(runs)) == 1


async def test_closing_the_engine_stops_everything():
    looping = Animation.model_validate({**WAVE, "loop": True})
    engine, _, _, _ = build_engine(looping)
    await engine.play("wave")
    await engine.aclose()
    assert engine.playing == ()
    assert await engine.play("wave") is None


# -- the handle adapter -------------------------------------------------------------------------------------------------------


async def test_the_handle_adapter_never_waits_for_motion_and_is_honest_about_audio():
    from tests.robot.conftest import RecordingRobot

    handle = RecordingRobot()
    player = HandlePlayer(handle)
    await player.set_expression("happy", 80)
    await player.turn(15)
    await player.play_sound("chirp")
    assert handle.arguments("set_expression")[0]["wait"] is False
    assert handle.arguments("turn")[0]["wait"] is False
    assert player.skipped_sounds == ["chirp"]  # no audio action in the vocabulary yet


async def test_a_behaviour_plays_animations_through_the_engine():
    """The adapter: a behaviour asks for a name, the engine turns it into a sequence."""
    from robot.behavior.engine import AnimatedRobot
    from tests.robot.conftest import RecordingRobot

    engine, player, _, _ = build_engine()
    robot = AnimatedRobot(RecordingRobot(), engine)
    assert await robot.play_animation("wave") is None
    await engine.wait_for("wave")
    assert player.commands[0] == "set_expression"
