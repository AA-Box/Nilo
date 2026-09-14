"""Robot vision: detections, tracking, the world model lifecycle, and what fails safely.

No camera, no model, no network. Frames come from committed fixtures rendered by the
simulator's own camera (`tests/robot/fixtures/vision/`), detections come from a scripted
fake, and every assertion is exact because nothing here samples anything.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from robot.behavior.tracking import decide_follow, decide_look_at
from robot.behavior.tuning import BehaviorTuning
from robot.events.bus import EventBus
from robot.events.types import (
    KnownPersonRecognized,
    ObjectDetected,
    ObjectLost,
    PersonDetected,
    PersonLost,
    UnknownPersonDetected,
    VisionFrameProcessed,
)
from robot.state.world import EntityType, ImagePoint
from robot.state.world_model import WorldModel
from robot.vision.faces import EmbeddingFaceRecognizer, FaceRegistry, cosine_similarity
from robot.vision.pipeline import McpFrameSource, StaticFrameSource, VisionPipeline
from robot.vision.providers import (
    FakeDetector,
    HeaderVisionProvider,
    NullDetector,
    OpenCVVisionProvider,
    VisionUnavailable,
    YoloObjectDetector,
)
from robot.vision.tracker import CentroidTracker
from robot.vision.types import (
    BoundingBox,
    Detection,
    DetectionKind,
    FaceIdentity,
    Frame,
    FrameRetention,
    position_from_box,
)
from tests.robot.conftest import ROBOT_ID

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vision"


def box(x: float, y: float, width: float = 0.2, height: float = 0.4) -> BoundingBox:
    return BoundingBox(x=x, y=y, width=width, height=height)


def person_at(x: float, y: float = 0.3, **kwargs) -> Detection:
    return Detection(kind=DetectionKind.PERSON, box=box(x, y, **kwargs), confidence=0.9, label="person")


def face_at(x: float, y: float = 0.2, *, embedding: list[float] | None = None) -> Detection:
    return Detection(
        kind=DetectionKind.FACE,
        box=box(x, y, 0.1, 0.1),
        confidence=0.8,
        attributes={"embedding": embedding} if embedding else {},
    )


def object_at(x: float, label: str = "cube") -> Detection:
    return Detection(kind=DetectionKind.OBJECT, box=box(x, 0.6, 0.1, 0.1), confidence=0.7, label=label)


def build_pipeline(detector=None, *, world=None, events=None, **kwargs) -> VisionPipeline:
    return VisionPipeline(
        ROBOT_ID,
        StaticFrameSource([Frame(data=b"\x89PNG\r\n\x1a\n" + b"0" * 32)]),
        world=world,
        events=events,
        detector=detector or NullDetector(),
        **kwargs,
    )


# -- coordinates ------------------------------------------------------------------------------


def test_boxes_are_normalized_and_convert_from_pixels():
    normalized = BoundingBox.from_pixels(80, 30, 40, 60, frame_width=160, frame_height=120)
    assert (normalized.x, normalized.y) == pytest.approx((0.5, 0.25))
    assert normalized.centre == pytest.approx((0.625, 0.5))
    with pytest.raises(ValueError, match="positive"):
        BoundingBox.from_pixels(1, 1, 1, 1, frame_width=0, frame_height=10)


def test_boxes_are_bounded_to_the_unit_square():
    with pytest.raises(Exception):
        BoundingBox(x=1.4, y=0.5, width=0.1, height=0.1)


def test_iou_and_centre_distance_behave():
    a = box(0.4, 0.4)
    assert a.iou(a) == pytest.approx(1.0)
    assert a.iou(box(0.9, 0.9)) == 0.0
    assert a.distance_to(box(0.4, 0.4)) == pytest.approx(0.0)


def test_a_bigger_box_is_estimated_as_closer():
    near = position_from_box(box(0.4, 0.3, 0.4, 0.8))
    far = position_from_box(box(0.4, 0.3, 0.1, 0.2))
    assert near.distance_mm < far.distance_mm
    # Left of centre is a positive bearing: the robot turns left towards it.
    assert position_from_box(box(0.1, 0.3)).bearing_deg > 0
    assert position_from_box(box(0.8, 0.3)).bearing_deg < 0


# -- the tracker ---------------------------------------------------------------------------------


def test_a_track_keeps_its_id_across_frames():
    tracker = CentroidTracker()
    active, lost = tracker.update([person_at(0.4)])
    assert [t.id for t in active] == ["person-1"]
    assert lost == ()
    active, _ = tracker.update([person_at(0.44)])
    assert [t.id for t in active] == ["person-1"]
    assert active[0].hits == 2


def test_two_people_get_two_ids_and_keep_them():
    tracker = CentroidTracker()
    tracker.update([person_at(0.2), person_at(0.8)])
    active, _ = tracker.update([person_at(0.82), person_at(0.22)])  # reported in the other order
    by_centre = {round(t.box.centre[0], 2): t.id for t in active}
    assert by_centre == {0.32: "person-1", 0.92: "person-2"}


def test_a_track_is_dropped_after_the_miss_timeout_and_not_before():
    tracker = CentroidTracker(max_misses=3)
    tracker.update([person_at(0.4)])
    for _ in range(3):
        active, lost = tracker.update([])
        assert lost == ()
    active, lost = tracker.update([])
    assert [t.id for t in lost] == ["person-1"]
    assert active == ()


def test_a_track_that_reappears_inside_the_timeout_is_the_same_person():
    tracker = CentroidTracker(max_misses=3)
    tracker.update([person_at(0.4)])
    tracker.update([])
    active, _ = tracker.update([person_at(0.42)])
    assert [t.id for t in active] == ["person-1"]
    assert active[0].misses == 0


def test_direction_and_approach_come_from_the_track_history():
    tracker = CentroidTracker()
    tracker.update([person_at(0.2)])
    active, _ = tracker.update([person_at(0.35)])
    assert active[0].direction() == "right"
    active, _ = tracker.update([person_at(0.2)])
    assert active[0].direction() == "left"

    growing = CentroidTracker()
    growing.update([person_at(0.4, width=0.1, height=0.2)])
    active, _ = growing.update([person_at(0.4, width=0.3, height=0.6)])
    assert active[0].approaching() is True


def test_detections_of_different_kinds_never_associate():
    tracker = CentroidTracker()
    tracker.update([person_at(0.4)])
    active, _ = tracker.update([object_at(0.4)])
    assert {t.id for t in active} == {"person-1", "object-1"}


def test_the_tracker_is_deterministic_whatever_order_detections_arrive_in():
    first, second = CentroidTracker(), CentroidTracker()
    first.update([person_at(0.2), person_at(0.7)])
    second.update([person_at(0.2), person_at(0.7)])
    a, _ = first.update([person_at(0.25), person_at(0.75)])
    b, _ = second.update([person_at(0.75), person_at(0.25)])
    assert [(t.id, round(t.box.x, 3)) for t in a] == [(t.id, round(t.box.x, 3)) for t in b]


# -- the pipeline ---------------------------------------------------------------------------------


async def test_a_detection_becomes_a_world_entity_with_normalized_coordinates():
    world = WorldModel()
    pipeline = build_pipeline(FakeDetector([[person_at(0.3)]]), world=world)
    result = await pipeline.process()
    assert result.ok
    entity = world.snapshot(ROBOT_ID).get("person-1")
    assert entity is not None
    assert entity.type is EntityType.PERSON
    assert entity.image_point is not None
    assert entity.image_point.x == pytest.approx(0.4)
    assert entity.position is not None and entity.position.distance_mm > 0


async def test_the_world_entity_is_updated_then_removed_across_the_track_lifecycle():
    world = WorldModel()
    detector = FakeDetector([[person_at(0.3)], [person_at(0.35)], [], [], [], []])
    pipeline = build_pipeline(detector, world=world, tracker=CentroidTracker(max_misses=2))

    await pipeline.process()
    first = world.snapshot(ROBOT_ID).get("person-1")
    await pipeline.process()
    second = world.snapshot(ROBOT_ID).get("person-1")
    assert first is not None and second is not None
    assert second.last_seen >= first.last_seen
    assert second.attributes["hits"] == 2

    for _ in range(4):
        await pipeline.process()
    assert world.snapshot(ROBOT_ID).get("person-1") is None


async def test_the_events_the_design_names_are_published():
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(
        (PersonDetected, PersonLost, ObjectDetected, ObjectLost, UnknownPersonDetected, KnownPersonRecognized),
        seen.append,
    )
    detector = FakeDetector([[person_at(0.3), object_at(0.7)], [], [], [], []])
    pipeline = build_pipeline(detector, events=bus, tracker=CentroidTracker(max_misses=1))
    await pipeline.process()
    await pipeline.process()
    await pipeline.process()
    await bus.drain()

    # Announced in track-id order, which is stable across runs — the order detections
    # happened to arrive in is not something a subscriber should be able to depend on.
    kinds = [type(event).__name__ for event in seen]
    assert set(kinds[:2]) == {"PersonDetected", "ObjectDetected"}
    assert "PersonLost" in kinds and "ObjectLost" in kinds
    assert kinds.index("PersonDetected") < kinds.index("PersonLost")
    detected = next(e for e in seen if isinstance(e, PersonDetected))
    assert detected.track_id == "person-1"
    assert 0.0 <= detected.x <= 1.0
    await bus.aclose()


async def test_a_person_still_in_frame_is_announced_once():
    bus = EventBus()
    announcements: list[PersonDetected] = []
    bus.subscribe(PersonDetected, announcements.append)
    pipeline = build_pipeline(FakeDetector([[person_at(0.3)]]), events=bus)
    for _ in range(5):
        await pipeline.process()
    await bus.drain()
    assert len(announcements) == 1
    await bus.aclose()


async def test_the_collaborators_a_caller_passes_in_are_the_ones_used():
    """An empty tracker is falsy — a pipeline that quietly built its own instead would
    silently ignore every tuning a caller made."""
    tracker = CentroidTracker(max_misses=99)
    registry = FaceRegistry()
    pipeline = build_pipeline(NullDetector(), tracker=tracker, faces=registry)
    assert pipeline.tracker is tracker
    assert pipeline.faces is registry


async def test_a_detector_failure_leaves_the_world_exactly_as_it_was():
    world = WorldModel()
    detector = FakeDetector([[person_at(0.3)], [person_at(0.3)]], fail_on=[1])
    pipeline = build_pipeline(detector, world=world)

    await pipeline.process()
    before = world.snapshot(ROBOT_ID)
    result = await pipeline.process()

    assert not result.ok
    assert "detection failed" in result.error
    assert world.snapshot(ROBOT_ID).entities.keys() == before.entities.keys()
    assert pipeline.metrics.failures == 1


async def test_a_camera_that_returns_nothing_is_a_failed_result_not_an_exception():
    pipeline = VisionPipeline(ROBOT_ID, StaticFrameSource([]))
    result = await pipeline.process()
    assert not result.ok
    assert "no frame" in result.error


async def test_a_capture_that_raises_is_caught():
    class Broken:
        async def capture(self):
            raise OSError("the camera is unplugged")

    pipeline = VisionPipeline(ROBOT_ID, Broken())
    result = await pipeline.process()
    assert not result.ok
    assert "capture failed" in result.error


async def test_the_null_detector_produces_a_pipeline_that_runs_and_finds_nothing():
    world = WorldModel()
    pipeline = build_pipeline(NullDetector(), world=world)
    result = await pipeline.process()
    assert result.ok
    assert result.detections == ()
    assert world.snapshot(ROBOT_ID).entities == {}


# -- metrics ------------------------------------------------------------------------------------------


async def test_latency_is_measured_per_frame_and_per_robot():
    pipeline = build_pipeline(FakeDetector([[person_at(0.3)]]))
    for _ in range(3):
        await pipeline.process()
    metrics = pipeline.metrics
    assert metrics.frames == 3
    assert metrics.detections == 3
    assert metrics.mean_latency_ms > 0
    assert metrics.max_latency_ms >= metrics.mean_latency_ms
    snapshot = metrics.snapshot()
    assert set(snapshot) == {
        "frames",
        "detections",
        "failures",
        "mean_latency_ms",
        "max_latency_ms",
        "last_latency_ms",
    }


async def test_every_pass_publishes_its_latency_even_when_it_failed():
    bus = EventBus()
    frames: list[VisionFrameProcessed] = []
    bus.subscribe(VisionFrameProcessed, frames.append)
    pipeline = build_pipeline(FakeDetector([[person_at(0.3)]], fail_on=[1]), events=bus)
    await pipeline.process()
    await pipeline.process()
    await bus.drain()
    assert len(frames) == 2
    assert frames[0].error == "" and frames[1].error
    assert all(event.latency_ms >= 0 for event in frames)
    await bus.aclose()


# -- faces -------------------------------------------------------------------------------------------------


def test_cosine_similarity_is_bounded_and_forgiving_of_bad_input():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_the_registry_holds_the_four_identity_fields():
    registry = FaceRegistry()
    identity = registry.enroll([1.0, 0.2, 0.1], person_id="ahmad", display_name="Ahmad")
    assert identity.person_id == "ahmad"
    assert identity.display_name == "Ahmad"
    assert identity.embedding_ref is not None
    assert identity.confidence == 1.0
    assert identity.known is True


def test_a_known_face_is_recognized_and_a_stranger_is_not():
    registry = FaceRegistry()
    registry.enroll([1.0, 0.1, 0.0], person_id="ahmad", display_name="Ahmad")
    assert registry.identify([0.99, 0.12, 0.0]) is not None
    assert registry.identify([0.0, 0.0, 1.0]) is None


def test_forgetting_a_person_removes_every_embedding_stored_for_them():
    registry = FaceRegistry()
    registry.enroll([1.0, 0.0], person_id="ahmad")
    registry.enroll([0.98, 0.05], person_id="ahmad")
    assert len(registry.get("ahmad").embeddings) == 2
    assert registry.forget("ahmad") is True
    assert registry.identify([1.0, 0.0]) is None
    assert registry.forget("ahmad") is False


async def test_a_recognized_face_is_announced_with_its_person_id():
    bus = EventBus()
    recognized: list[KnownPersonRecognized] = []
    unknown: list[UnknownPersonDetected] = []
    bus.subscribe(KnownPersonRecognized, recognized.append)
    bus.subscribe(UnknownPersonDetected, unknown.append)

    registry = FaceRegistry()
    registry.enroll([1.0, 0.1, 0.0], person_id="ahmad", display_name="Ahmad")
    pipeline = build_pipeline(
        FakeDetector([[face_at(0.4, embedding=[0.99, 0.11, 0.0])]]),
        events=bus,
        faces=registry,
        recognizer=EmbeddingFaceRecognizer(registry),
    )
    await pipeline.process()
    await bus.drain()
    assert [e.person_id for e in recognized] == ["ahmad"]
    assert recognized[0].display_name == "Ahmad"
    assert recognized[0].embedding_ref is not None
    assert unknown == []
    await bus.aclose()


async def test_an_unrecognized_face_is_announced_as_unknown_not_as_an_error():
    bus = EventBus()
    unknown: list[UnknownPersonDetected] = []
    bus.subscribe(UnknownPersonDetected, unknown.append)
    pipeline = build_pipeline(FakeDetector([[face_at(0.4, embedding=[0.0, 0.0, 1.0])]]), events=bus)
    result = await pipeline.process()
    await bus.drain()
    assert result.ok
    assert len(unknown) == 1
    await bus.aclose()


async def test_enrolling_unknown_faces_is_off_by_default():
    registry = FaceRegistry()
    recognizer = EmbeddingFaceRecognizer(registry)
    pipeline = build_pipeline(
        FakeDetector([[face_at(0.4, embedding=[0.3, 0.9, 0.1])]]), recognizer=recognizer
    )
    await pipeline.process()
    assert len(registry) == 0

    learning = EmbeddingFaceRecognizer(registry, enroll_unknown=True)
    pipeline = build_pipeline(
        FakeDetector([[face_at(0.4, embedding=[0.3, 0.9, 0.1])]]), recognizer=learning
    )
    await pipeline.process()
    assert len(registry) == 1


async def test_a_face_with_no_embedding_is_simply_unidentified():
    pipeline = build_pipeline(FakeDetector([[face_at(0.4)]]))
    result = await pipeline.process()
    assert result.ok
    assert result.detections[0].identity is None


# -- frame retention ------------------------------------------------------------------------------------------


async def test_frames_are_ephemeral_by_default():
    pipeline = build_pipeline(FakeDetector([[person_at(0.3)]]))
    result = await pipeline.process()
    assert result.frame is not None
    assert result.frame.data is None      # the bytes are gone
    assert pipeline.last_frame is None    # and nothing was kept


async def test_last_only_retention_keeps_exactly_one_frame_in_memory():
    pipeline = build_pipeline(NullDetector(), retention=FrameRetention.LAST_ONLY)
    await pipeline.process()
    assert pipeline.last_frame is not None
    assert pipeline.last_frame.data is not None


async def test_persisting_frames_requires_a_directory_and_writes_there(tmp_path: Path):
    pipeline = build_pipeline(
        NullDetector(), retention=FrameRetention.PERSIST, retention_dir=tmp_path
    )
    await pipeline.process()
    written = list(tmp_path.glob("*.png"))
    assert len(written) == 1

    without_dir = build_pipeline(NullDetector(), retention=FrameRetention.PERSIST)
    result = await without_dir.process()
    assert result.frame is not None and result.frame.data is None


# -- frames and providers ------------------------------------------------------------------------------------------


def test_the_committed_fixtures_exist_and_are_real_images():
    images = sorted(FIXTURES.glob("*.png"))
    assert len(images) >= 8
    for path in images:
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name


def test_the_header_provider_reads_dimensions_with_no_dependencies():
    frame = Frame(data=(FIXTURES / "03_person_centre.png").read_bytes())
    decoded = HeaderVisionProvider().decode(frame)
    assert (decoded.width, decoded.height) == (160, 120)


def test_an_unreadable_format_leaves_the_dimensions_unknown_rather_than_failing():
    decoded = HeaderVisionProvider().decode(Frame(data=b"not an image"))
    assert (decoded.width, decoded.height) == (0, 0)


async def test_a_fixture_directory_can_drive_the_pipeline():
    source = StaticFrameSource.from_directory(FIXTURES)
    pipeline = VisionPipeline(ROBOT_ID, source, detector=NullDetector())
    result = await pipeline.process()
    assert result.ok
    assert source.calls == 1


def test_opencv_is_optional_and_falls_back_to_the_header_provider():
    provider = OpenCVVisionProvider()
    frame = Frame(data=(FIXTURES / "02_person_left.png").read_bytes())
    decoded = provider.decode(frame)
    assert (decoded.width, decoded.height) == (160, 120)  # whether or not cv2 is installed


def test_yolo_is_optional_and_says_so_clearly():
    detector = YoloObjectDetector()
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        with pytest.raises(VisionUnavailable, match="ultralytics is not installed"):
            detector.load()


async def test_the_device_reply_shape_decodes_into_a_frame():
    payload = json.dumps(
        {
            "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nbody").decode(),
            "mime_type": "image/png",
            "width": 160,
            "height": 120,
        }
    )
    calls: list[tuple[str, dict]] = []

    async def call_tool(name, arguments, timeout=None):
        calls.append((name, arguments))
        return payload

    frame = await McpFrameSource(call_tool).capture()
    assert frame is not None
    assert frame.width == 160
    assert frame.data is not None and frame.data.startswith(b"\x89PNG")
    assert calls == [("robot_camera_capture", {})]


@pytest.mark.parametrize("reply", ["not json", "{}", '{"image_base64": ""}', '{"image_base64": "!!!"}'])
async def test_a_device_reply_with_no_usable_image_is_none_not_a_crash(reply: str):
    async def call_tool(name, arguments, timeout=None):
        return reply

    assert await McpFrameSource(call_tool).capture() is None


# -- look-at and follow ---------------------------------------------------------------------------------------------


def test_look_at_ignores_a_target_that_is_already_centred():
    tuning = BehaviorTuning()
    decision = decide_look_at(
        ImagePoint(x=0.52, y=0.5), now=10.0, last_command_at=None, tuning=tuning
    )
    assert not decision
    assert decision.reason == "already centred"


def test_look_at_moves_a_fraction_of_the_error():
    tuning = BehaviorTuning()
    decision = decide_look_at(ImagePoint(x=0.1, y=0.5), now=10.0, last_command_at=None, tuning=tuning)
    assert decision
    assert decision.x_pct == round((0.5 + (0.1 - 0.5) * tuning.look_at_gain) * 100)
    assert 10 < decision.x_pct < 50


def test_look_at_is_rate_limited():
    tuning = BehaviorTuning()
    point = ImagePoint(x=0.1, y=0.5)
    assert decide_look_at(point, now=10.0, last_command_at=9.9, tuning=tuning).reason == "rate limited"
    fresh = decide_look_at(point, now=10.0, last_command_at=10.0 - tuning.look_at_min_interval_s, tuning=tuning)
    assert fresh


def test_repeated_look_at_converges_instead_of_oscillating():
    """The jitter test: feeding the commanded point back in must settle, not ring."""
    tuning = BehaviorTuning()
    x = 0.05
    commands = []
    for step in range(6):
        decision = decide_look_at(
            ImagePoint(x=x, y=0.5), now=step * 10.0, last_command_at=None, tuning=tuning
        )
        if not decision:
            break
        commands.append(decision.x_pct)
        x = decision.x_pct / 100  # the head moved there; the target is now that much closer
    assert commands == sorted(commands)          # monotonic, never overshooting past centre
    assert all(value <= 50 for value in commands)


def test_follow_turns_towards_an_off_centre_target_and_drives_to_the_stop_distance():
    tuning = BehaviorTuning()
    left = decide_follow(offset_x=-0.4, distance_mm=2000, now=10.0, last_command_at=None, tuning=tuning)
    assert left.turn_deg > 0                     # target left of centre, so turn left
    assert abs(left.turn_deg) <= tuning.follow_max_turn_deg
    assert 0 < left.move_mm <= tuning.follow_step_mm

    right = decide_follow(offset_x=0.4, distance_mm=2000, now=10.0, last_command_at=None, tuning=tuning)
    assert right.turn_deg == -left.turn_deg


def test_follow_holds_station_inside_the_stop_band():
    tuning = BehaviorTuning()
    decision = decide_follow(
        offset_x=0.0,
        distance_mm=tuning.follow_stop_distance_mm,
        now=10.0,
        last_command_at=None,
        tuning=tuning,
    )
    assert not decision.acts
    assert decision.reason == "holding station"


def test_follow_backs_off_when_the_target_is_too_close():
    tuning = BehaviorTuning()
    decision = decide_follow(
        offset_x=0.0, distance_mm=200, now=10.0, last_command_at=None, tuning=tuning
    )
    assert decision.move_mm < 0


def test_follow_stops_when_a_sensor_says_the_way_is_blocked():
    decision = decide_follow(
        offset_x=0.3, distance_mm=3000, now=10.0, last_command_at=None, tuning=BehaviorTuning(), blocked=True
    )
    assert decision.stop is True
    assert decision.move_mm == 0


def test_follow_is_rate_limited():
    tuning = BehaviorTuning()
    decision = decide_follow(
        offset_x=0.4, distance_mm=3000, now=10.0, last_command_at=9.95, tuning=tuning
    )
    assert not decision.acts
    assert decision.reason == "rate limited"


async def test_the_follow_behaviour_drives_from_a_tracked_target():
    """End to end: a tracked person in the world model becomes a bounded drive command."""
    from robot.behavior.base import AutonomyMode
    from robot.state.world import Position, person
    from tests.robot.conftest import WORLD_T0, behavior_world, make_scheduler

    scheduler, robot, clock = make_scheduler(
        tuning=BehaviorTuning(score_jitter=0.0), mode=AutonomyMode.FULL
    )
    target = person(
        "person-1",
        known=True,
        position=Position(distance_mm=2000, bearing_deg=0.0),
        image_point=ImagePoint(x=0.25, y=0.5, width=0.2, height=0.4),
        now=WORLD_T0,
    )
    world = behavior_world(entities=[target]).attend_to("person-1", now=WORLD_T0)

    import asyncio

    await scheduler.tick(world)          # greeting first
    await asyncio.sleep(0)
    clock.advance(2.0)
    decision = await scheduler.tick(world)
    await asyncio.sleep(0)
    assert decision.selected in {"follow_person", "approach_person", "look_at_person"}
    if decision.selected == "follow_person":
        assert robot.arguments("turn")[0]["angle_deg"] > 0     # target left of centre
        assert robot.arguments("move")[0]["distance_mm"] > 0
        assert robot.arguments("follow") == ()                 # closed here, not on the device
    await scheduler.aclose()
