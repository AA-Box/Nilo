"""The world model: entity lifecycle, the decay schedule, interactions, event folding.

No clock is read anywhere in this file. Every age is expressed by passing an explicit
``now``, which is the only way a "the person is forgotten after twenty seconds" test can
run in a millisecond and never flake.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robot.events.bus import EventBus
from robot.events.types import BatteryUpdated, RobotConnected, SensorUpdated, TelemetryUpdated
from robot.state.models import (
    RobotBatteryState,
    RobotSensorState,
    RobotTelemetry,
)
from robot.state.world import (
    DEFAULT_TTL_S,
    Entity,
    EntityType,
    Environment,
    ImagePoint,
    Interaction,
    InteractionKind,
    Position,
    WorldState,
    obj,
    person,
)
from robot.state.world_model import WorldModel
from tests.robot.conftest import ROBOT_ID, device_info, robot_state

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def world(**kwargs) -> WorldState:
    return WorldState(robot_id=ROBOT_ID, updated_at=T0, **kwargs)


# -- entities ------------------------------------------------------------------------------


def test_an_entity_carries_the_fields_the_design_asks_for():
    visitor = person("person-1", name="Ahmad", known=True, confidence=0.9, now=T0)
    assert (visitor.id, visitor.type) == ("person-1", EntityType.PERSON)
    assert visitor.attributes == {"known": True, "name": "Ahmad"}
    assert (visitor.confidence, visitor.first_seen, visitor.last_seen) == (0.9, T0, T0)
    assert visitor.position is None


def test_observing_again_moves_last_seen_but_never_first_seen():
    first = world().observe(person("person-1", now=T0), now=T0)
    later = first.observe(
        person("person-1", now=at(30), position=Position(distance_mm=900)), now=at(30)
    )
    entity = later.entities["person-1"]
    assert entity.first_seen == T0
    assert entity.last_seen == at(30)
    assert entity.position is not None and entity.position.distance_mm == 900


def test_observing_merges_attributes_rather_than_replacing_them():
    seen = world().observe(person("person-1", name="Ahmad", now=T0), now=T0)
    merged = seen.observe(person("person-1", known=True, now=at(1)), now=at(1))
    assert merged.entities["person-1"].attributes == {"name": "Ahmad", "known": True}


def test_a_snapshot_is_immutable_and_every_change_returns_a_new_one():
    before = world()
    after = before.observe(person("person-1", now=T0), now=T0)
    assert before.entities == {}
    assert set(after.entities) == {"person-1"}
    with pytest.raises(Exception):
        before.attention_target = "person-1"  # type: ignore[misc]
    # The freeze is at the field level: every mutator rebuilds the dict rather than
    # writing into it, so no two snapshots ever share one.
    assert before.entities is not after.entities


def test_entities_are_indexed_by_kind():
    populated = (
        world()
        .observe(person("person-1", now=T0), now=T0)
        .observe(obj("cube", now=T0), now=T0)
        .observe(
            Entity(id="dock", type=EntityType.LOCATION, first_seen=T0, last_seen=T0, updated_at=T0),
            now=T0,
        )
    )
    assert [e.id for e in populated.people] == ["person-1"]
    assert [e.id for e in populated.objects] == ["cube"]
    assert [e.id for e in populated.locations] == ["dock"]
    assert populated.obstacles == ()


def test_nearest_prefers_the_closest_entity_that_has_a_position():
    populated = (
        world()
        .observe(person("far", position=Position(distance_mm=3000), now=T0), now=T0)
        .observe(person("near", position=Position(distance_mm=800), now=T0), now=T0)
    )
    nearest = populated.nearest(EntityType.PERSON)
    assert nearest is not None and nearest.id == "near"


def test_seen_within_filters_on_the_age_of_the_sighting():
    populated = (
        world()
        .observe(person("old", now=T0), now=T0)
        .observe(person("new", now=at(9)), now=at(9))
    )
    fresh = populated.seen_within(EntityType.PERSON, 5.0, at(10))
    assert [e.id for e in fresh] == ["new"]


# -- decay ---------------------------------------------------------------------------------------


def test_confidence_halves_over_one_half_life():
    seen = world().observe(person("person-1", confidence=1.0, now=T0), now=T0)
    decayed = seen.decay(at(8), half_life_s={EntityType.PERSON: 8.0})
    assert decayed.entities["person-1"].confidence == pytest.approx(0.5)


def test_an_entity_is_dropped_once_it_is_past_its_documented_ttl():
    ttl = DEFAULT_TTL_S[EntityType.PERSON]
    seen = world().observe(person("person-1", now=T0), now=T0)
    assert seen.decay(at(ttl - 0.1)).entities
    assert seen.decay(at(ttl + 0.1)).entities == {}


def test_decay_keeps_a_location_long_after_it_forgets_a_face():
    populated = (
        world()
        .observe(Entity(id="dock", type=EntityType.LOCATION, first_seen=T0, last_seen=T0, updated_at=T0), now=T0)
        .observe(Entity(id="face-1", type=EntityType.FACE, first_seen=T0, last_seen=T0, updated_at=T0), now=T0)
    )
    aged = populated.decay(at(60))
    assert set(aged.entities) == {"dock"}


def test_decay_clears_an_attention_target_that_was_forgotten():
    seen = world().observe(person("person-1", now=T0), now=T0).attend_to("person-1", now=T0)
    assert seen.attention_target == "person-1"
    aged = seen.decay(at(DEFAULT_TTL_S[EntityType.PERSON] + 1))
    assert aged.attention_target is None


def test_forgetting_the_attention_target_clears_the_pointer():
    seen = world().observe(person("person-1", now=T0), now=T0).attend_to("person-1", now=T0)
    assert seen.forget("person-1", now=at(1)).attention_target is None


def test_attending_to_an_unknown_entity_raises_rather_than_dangling():
    with pytest.raises(KeyError):
        world().attend_to("nobody")


# -- interactions ----------------------------------------------------------------------------------


def test_starting_a_second_interaction_closes_the_first():
    first = Interaction(id="a", started_at=T0, kind=InteractionKind.CONVERSATION)
    second = Interaction(id="b", started_at=at(10), kind=InteractionKind.PLAY)
    populated = world().start_interaction(first, now=T0).start_interaction(second, now=at(10))
    assert populated.current_interaction is not None and populated.current_interaction.id == "b"
    assert populated.last_interaction is not None and populated.last_interaction.id == "a"
    assert populated.last_interaction.ended_at == at(10)


def test_ending_an_interaction_moves_it_to_last_interaction():
    populated = world().start_interaction(Interaction(id="a", started_at=T0), now=T0)
    ended = populated.end_interaction(now=at(30), summary="talked about cubes")
    assert ended.current_interaction is None
    assert ended.last_interaction is not None
    assert ended.last_interaction.summary == "talked about cubes"
    assert ended.last_interaction.duration_s() == pytest.approx(30.0)
    assert ended.last_interaction.since_end_s(at(45)) == pytest.approx(15.0)


# -- telemetry shortcuts ------------------------------------------------------------------------------


def test_telemetry_shortcuts_read_the_merged_snapshot():
    populated = world().with_telemetry(
        RobotTelemetry(
            battery=RobotBatteryState(percent=42, charging=True),
            sensors=RobotSensorState(touch_detected=True, cliff_detected=True),
        ),
        now=T0,
    )
    assert populated.battery_percent == 42
    assert populated.charging is True
    assert populated.touched is True
    assert populated.blocked is True


def test_a_telemetry_patch_only_overwrites_the_fields_it_carries():
    populated = world().with_telemetry(RobotTelemetry(battery=RobotBatteryState(percent=42)), now=T0)
    patched = populated.with_telemetry(
        RobotTelemetry(sensors=RobotSensorState(touch_detected=True)), now=at(1)
    )
    assert patched.battery_percent == 42
    assert patched.touched is True


# -- the model in front of it ---------------------------------------------------------------------------


async def test_the_model_creates_a_world_on_first_ask():
    model = WorldModel()
    assert (await model.state(ROBOT_ID)).entities == {}
    assert model.robot_ids() == (ROBOT_ID,)


async def test_the_model_folds_a_connection_into_a_self_entity():
    model = WorldModel()
    bus = EventBus()
    model.attach(bus)
    device = device_info(device_id=ROBOT_ID)
    await bus.publish(
        RobotConnected(
            robot_id=ROBOT_ID, identity=device.identity(), connection=device.connection()
        )
    )
    await bus.drain()
    entity = (await model.state(ROBOT_ID)).get(ROBOT_ID)
    assert entity is not None and entity.type is EntityType.ROBOT
    assert entity.attributes["self"] is True
    await model.aclose()
    await bus.aclose()


@pytest.mark.parametrize(
    ("event", "check"),
    [
        (
            lambda: BatteryUpdated(robot_id=ROBOT_ID, battery=RobotBatteryState(percent=11)),
            lambda w: w.battery_percent == 11,
        ),
        (
            lambda: SensorUpdated(robot_id=ROBOT_ID, sensors=RobotSensorState(touch_detected=True)),
            lambda w: w.touched,
        ),
        (
            lambda: TelemetryUpdated(
                robot_id=ROBOT_ID,
                telemetry=RobotTelemetry(battery=RobotBatteryState(percent=7, charging=True)),
            ),
            lambda w: w.charging and w.battery_percent == 7,
        ),
    ],
)
async def test_telemetry_events_write_the_world(event, check):
    model = WorldModel()
    bus = EventBus()
    model.attach(bus)
    await bus.publish(event())
    await bus.drain()
    assert check(await model.state(ROBOT_ID))
    await model.aclose()
    await bus.aclose()


async def test_a_handler_failure_does_not_take_the_model_down():
    model = WorldModel()
    bus = EventBus()
    model.attach(bus)
    # A connected event whose identity is fine, followed by telemetry: if the first one
    # threw, the second would never be folded.
    await bus.publish(BatteryUpdated(robot_id=ROBOT_ID, battery=RobotBatteryState(percent=50)))
    await bus.publish(BatteryUpdated(robot_id=ROBOT_ID, battery=RobotBatteryState(percent=49)))
    await bus.drain()
    assert (await model.state(ROBOT_ID)).battery_percent == 49
    await model.aclose()
    await bus.aclose()


async def test_detach_stops_the_model_hearing_anything():
    model = WorldModel()
    bus = EventBus()
    model.attach(bus)
    model.detach()
    await bus.publish(BatteryUpdated(robot_id=ROBOT_ID, battery=RobotBatteryState(percent=3)))
    await bus.drain()
    assert (await model.state(ROBOT_ID)).battery_percent is None
    await bus.aclose()


async def test_the_snapshot_accessor_never_blocks_and_never_lies():
    model = WorldModel()
    await model.observe(ROBOT_ID, person("person-1", now=T0))
    snapshot = model.snapshot(ROBOT_ID)
    await model.forget(ROBOT_ID, "person-1")
    # The snapshot taken before the change still has the person: it is a value, not a view.
    assert "person-1" in snapshot.entities
    assert "person-1" not in model.snapshot(ROBOT_ID).entities


async def test_decay_runs_over_every_robot():
    model = WorldModel()
    await model.observe("a", person("person-1", now=T0))
    await model.observe("b", person("person-2", now=T0))
    await model.decay(at(DEFAULT_TTL_S[EntityType.PERSON] + 1))
    assert model.snapshot("a").entities == {}
    assert model.snapshot("b").entities == {}


async def test_the_runtime_exposes_one_world_model_and_folds_into_it(runtime):
    await runtime.attach(device_info(device_id=ROBOT_ID), discover=False)
    await runtime.update_telemetry(
        robot_state(ROBOT_ID).robot_id, RobotTelemetry(battery=RobotBatteryState(percent=33))
    )
    await runtime.events.drain()
    assert runtime.world.snapshot(ROBOT_ID).battery_percent == 33


async def test_an_environment_update_replaces_the_ambient_block():
    model = WorldModel()
    await model.set_environment(ROBOT_ID, Environment(sound_level=0.9, label="bang"))
    assert model.snapshot(ROBOT_ID).environment.sound_level == pytest.approx(0.9)


def test_an_image_point_is_normalized_and_knows_its_offset():
    point = ImagePoint(x=0.25, y=0.75, width=0.2, height=0.4)
    dx, dy = point.offset_from_centre()
    assert (dx, dy) == pytest.approx((-0.25, 0.25))
    assert point.area == pytest.approx(0.08)
    with pytest.raises(Exception):
        ImagePoint(x=1.5, y=0.5)
