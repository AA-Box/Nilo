"""The pipeline: one snapshot in, world state and events out.

    capture → decode → preprocess → detect → recognize → track → world model → events

One pass per call. Nothing here is a video loop: :meth:`VisionPipeline.process` takes one
frame and returns, and :meth:`VisionPipeline.run` is a thin timer over it for a deployment
that wants continuous perception. Snapshots are the contract because the device already
has a capture tool, because a still image is what a vision endpoint accepts, and because a
design that needs continuous frames cannot run on hardware that cannot send them.

Properties that are load-bearing:

* **Frames are ephemeral by default.** The image bytes are dropped the moment detection is
  done. Keeping them is a configuration choice somebody made, not a default they inherited.
* **A failure is a result, not an exception.** A detector that raises, a camera that
  returns nothing, a model file that moved — each produces a :class:`VisionResult` with an
  ``error``, a published frame event, and a world model that is left exactly as it was.
  Perception failing must not take a robot's autonomy with it.
* **Latency is measured, not assumed.** Every pass records its own time, and the metrics
  are readable per robot.
* **Nothing here commands the robot.** ``robot/vision`` may not import ``robot/actions``
  or ``robot/behavior``; it writes the world model, and the behaviour engine reads it. The
  look-at and follow controllers live one layer up, where the action layer is reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from robot.events.bus import EventBus
from robot.events.types import (
    FaceDetected,
    KnownPersonRecognized,
    ObjectDetected,
    ObjectLost,
    PersonDetected,
    PersonLost,
    RobotEvent,
    UnknownPersonDetected,
    VisionFrameProcessed,
)
from robot.state.world import Entity, EntityType
from robot.state.world_model import WorldModel
from robot.vision.faces import EmbeddingFaceRecognizer, FaceRegistry
from robot.vision.providers import (
    FaceRecognizer,
    FrameSource,
    HeaderVisionProvider,
    NullDetector,
    ObjectDetector,
    VisionProvider,
)
from robot.vision.tracker import CentroidTracker, Track, Tracker
from robot.vision.types import (
    Detection,
    DetectionKind,
    Frame,
    FrameRetention,
    VisionMetrics,
    VisionResult,
    position_from_box,
)

logger = logging.getLogger(__name__)

#: How often :meth:`VisionPipeline.run` captures when it owns the timer. Snapshots are a
#: round trip to the device, so this is a considered interval rather than a frame rate.
DEFAULT_INTERVAL_S = 1.0


class VisionPipeline:
    """Perception for one robot: from a camera frame to entities in the world model.

    Every collaborator is injected and every one has a default that needs nothing
    installed, so a pipeline can be built in a test with one line and no camera.
    """

    def __init__(
        self,
        robot_id: str,
        source: FrameSource,
        *,
        world: WorldModel | None = None,
        events: EventBus | None = None,
        provider: VisionProvider | None = None,
        detector: ObjectDetector | None = None,
        face_detector: Any | None = None,
        recognizer: FaceRecognizer | None = None,
        faces: FaceRegistry | None = None,
        tracker: Tracker | None = None,
        retention: FrameRetention = FrameRetention.EPHEMERAL,
        retention_dir: str | Path | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.source = source
        self.world = world
        self.events = events
        # `x is None` rather than `x or Default()` throughout: an empty tracker (or
        # registry, or library) is falsy because it has a __len__, and `or` would silently
        # throw away the one the caller passed in.
        self.provider: VisionProvider = HeaderVisionProvider() if provider is None else provider
        self.detector: ObjectDetector = NullDetector() if detector is None else detector
        self.face_detector = face_detector
        self.faces = FaceRegistry() if faces is None else faces
        self.recognizer: FaceRecognizer = (
            EmbeddingFaceRecognizer(self.faces) if recognizer is None else recognizer
        )
        self.tracker: Tracker = CentroidTracker() if tracker is None else tracker
        self.retention = retention
        self.retention_dir = Path(retention_dir) if retention_dir is not None else None
        self.metrics = VisionMetrics()
        self.last_frame: Frame | None = None
        self.last_result: VisionResult | None = None
        self._known_tracks: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # -- one pass ------------------------------------------------------------------------------

    async def process(self, frame: Frame | None = None) -> VisionResult:
        """Capture (or take) one frame and fold it all the way into the world model."""
        started = time.perf_counter()
        try:
            picture = frame if frame is not None else await self.source.capture()
        except Exception as exc:
            return await self._failed(started, f"capture failed: {type(exc).__name__}: {exc}")
        if picture is None:
            return await self._failed(started, "the camera returned no frame")

        try:
            picture = self.provider.decode(picture)
            picture = self.provider.preprocess(picture)
        except Exception as exc:
            return await self._failed(started, f"decode failed: {type(exc).__name__}: {exc}")

        try:
            detections = list(await self.detector.detect(picture))
            if self.face_detector is not None:
                detections.extend(await self.face_detector.detect_faces(picture))
        except Exception as exc:
            # A detector that raises leaves the world exactly as it was. It does not
            # produce "nobody is here", which would make a person vanish because a model
            # file moved.
            return await self._failed(started, f"detection failed: {type(exc).__name__}: {exc}")

        detections = await self._recognize(picture, detections)
        active, lost = self.tracker.update(detections)
        await self._update_world(active, lost)
        await self._announce(active, lost)

        latency_ms = (time.perf_counter() - started) * 1000
        self.metrics.record(latency_ms, len(detections))
        kept = self._retain(picture)
        result = VisionResult(
            robot_id=self.robot_id,
            detections=tuple(detections),
            frame=kept,
            latency_ms=round(latency_ms, 3),
            provider=getattr(self.detector, "name", "unknown"),
        )
        self.last_result = result
        await self._publish(
            VisionFrameProcessed(
                robot_id=self.robot_id,
                latency_ms=result.latency_ms,
                detections=len(detections),
                tracks=len(active),
                provider=result.provider,
            )
        )
        return result

    async def run(self, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        """Process on a timer until cancelled. One frame at a time, never overlapping."""
        while not self._closed:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - process() already catches everything
                logger.exception("robot %s: vision pass failed", self.robot_id)
            await asyncio.sleep(interval_s)

    def start(self, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(interval_s=interval_s), name=f"robot-vision-{self.robot_id}")

    async def aclose(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.last_frame = None

    # -- the stages --------------------------------------------------------------------------------

    async def _recognize(self, frame: Frame, detections: Sequence[Detection]) -> list[Detection]:
        """Put names to face detections. A recognizer that fails leaves them unnamed."""
        resolved: list[Detection] = []
        for detection in detections:
            if detection.kind is not DetectionKind.FACE or detection.identity is not None:
                resolved.append(detection)
                continue
            try:
                identity = await self.recognizer.recognize(frame, detection)
            except Exception as exc:
                logger.warning("robot %s: face recognition failed: %s", self.robot_id, exc)
                identity = None
            resolved.append(detection if identity is None else detection.model_copy(update={"identity": identity}))
        return resolved

    async def _update_world(self, active: Sequence[Track], lost: Sequence[Track]) -> None:
        """Write the tracks into the world model. The only place vision writes state."""
        if self.world is None:
            return
        for track in active:
            await self.world.observe(self.robot_id, self._entity(track))
        for track in lost:
            await self.world.forget(self.robot_id, track.id)

    def _entity(self, track: Track) -> Entity:
        """One track as a world entity. Identity, if any, travels as attributes."""
        attributes: dict[str, Any] = {"track": track.id, "hits": track.hits}
        if track.label:
            attributes["label"] = track.label
        if track.kind is DetectionKind.PERSON:
            attributes["known"] = track.identity is not None
            if track.identity is not None:
                attributes["person_id"] = track.identity.person_id
                attributes["name"] = track.identity.name
        if track.identity is not None:
            attributes["embedding_ref"] = track.identity.embedding_ref
            attributes["recognition_confidence"] = track.identity.confidence
        return Entity(
            id=track.id,
            type=track.kind.entity_type,
            attributes=attributes,
            confidence=track.confidence,
            position=position_from_box(track.box),
            image_point=track.box.to_image_point(),
        )

    async def _announce(self, active: Sequence[Track], lost: Sequence[Track]) -> None:
        """Publish what changed. New tracks are news; a track seen again is not."""
        for track in active:
            first_time = track.id not in self._known_tracks
            if first_time:
                self._known_tracks.add(track.id)
            await self._announce_track(track, first_time=first_time)
        for track in lost:
            self._known_tracks.discard(track.id)
            if track.kind is DetectionKind.PERSON:
                await self._publish(
                    PersonLost(
                        robot_id=self.robot_id,
                        track_id=track.id,
                        person_id=track.identity.person_id if track.identity else None,
                        frames_missing=track.misses,
                    )
                )
            elif track.kind is DetectionKind.OBJECT:
                await self._publish(
                    ObjectLost(robot_id=self.robot_id, track_id=track.id, label=track.label or "object")
                )

    async def _announce_track(self, track: Track, *, first_time: bool) -> None:
        if not first_time:
            return
        centre_x, centre_y = track.box.centre
        if track.kind is DetectionKind.PERSON:
            await self._publish(
                PersonDetected(
                    robot_id=self.robot_id,
                    track_id=track.id,
                    x=centre_x,
                    y=centre_y,
                    area=track.box.area,
                    confidence=track.confidence,
                    person_id=track.identity.person_id if track.identity else None,
                    display_name=track.identity.name if track.identity else "",
                )
            )
            return
        if track.kind is DetectionKind.FACE:
            await self._publish(
                FaceDetected(
                    robot_id=self.robot_id, track_id=track.id, x=centre_x, y=centre_y, confidence=track.confidence
                )
            )
            if track.identity is not None:
                await self._publish(
                    KnownPersonRecognized(
                        robot_id=self.robot_id,
                        track_id=track.id,
                        person_id=track.identity.person_id,
                        display_name=track.identity.name,
                        confidence=track.identity.confidence,
                        embedding_ref=track.identity.embedding_ref,
                    )
                )
            else:
                await self._publish(
                    UnknownPersonDetected(
                        robot_id=self.robot_id,
                        track_id=track.id,
                        x=centre_x,
                        y=centre_y,
                        confidence=track.confidence,
                    )
                )
            return
        await self._publish(
            ObjectDetected(
                robot_id=self.robot_id,
                track_id=track.id,
                label=track.label or "object",
                x=centre_x,
                y=centre_y,
                area=track.box.area,
                confidence=track.confidence,
            )
        )

    def _retain(self, frame: Frame) -> Frame:
        """Apply the retention policy. The default drops the image bytes and keeps nothing."""
        if self.retention is FrameRetention.EPHEMERAL:
            self.last_frame = None
            return frame.without_data()
        if self.retention is FrameRetention.LAST_ONLY:
            self.last_frame = frame
            return frame
        if self.retention_dir is None:
            logger.warning(
                "robot %s: frame retention is PERSIST with no directory; keeping nothing", self.robot_id
            )
            return frame.without_data()
        self.retention_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".jpg" if "jpeg" in frame.media_type else ".png"
        path = self.retention_dir / f"{frame.captured_at:%Y%m%dT%H%M%S%f}{suffix}"
        path.write_bytes(frame.data or b"")
        self.last_frame = frame
        return frame

    async def _failed(self, started: float, message: str) -> VisionResult:
        latency_ms = (time.perf_counter() - started) * 1000
        self.metrics.record(latency_ms, 0, failed=True)
        logger.warning("robot %s: vision pass failed: %s", self.robot_id, message)
        result = VisionResult(
            robot_id=self.robot_id,
            latency_ms=round(latency_ms, 3),
            provider=getattr(self.detector, "name", "unknown"),
            error=message,
        )
        self.last_result = result
        await self._publish(
            VisionFrameProcessed(
                robot_id=self.robot_id,
                latency_ms=result.latency_ms,
                provider=result.provider,
                error=message,
            )
        )
        return result

    async def _publish(self, event: RobotEvent) -> None:
        if self.events is None:
            return
        try:
            await self.events.publish(event)
        except Exception as exc:
            logger.debug("robot %s: could not publish %s: %s", self.robot_id, event.name, exc)

    def __repr__(self) -> str:
        return (
            f"<VisionPipeline {self.robot_id} detector={getattr(self.detector, 'name', '?')} "
            f"frames={self.metrics.frames} failures={self.metrics.failures}>"
        )


class CallableFrameSource:
    """Wraps any awaitable that returns bytes into a :class:`FrameSource`.

    The runtime binds this to the device's capture tool. Vision never calls a device
    itself — the layering forbids it, and the seam is one lambda.
    """

    def __init__(self, capture: Any, *, media_type: str = "image/png", source: str = "camera") -> None:
        self._capture = capture
        self.media_type = media_type
        self.source = source

    async def capture(self) -> Frame | None:
        data = await self._capture()
        if not data:
            return None
        return Frame(data=data, media_type=self.media_type, source=self.source)


class McpFrameSource:
    """Captures through the device's own camera tool.

    Takes a ``call_tool(name, arguments) -> str`` callable rather than a runtime, so
    ``robot/vision`` still imports nothing that can command a robot: the caller supplies
    the one function that can, and what comes back is an image.

    The reply shape is the device-MCP convention — a JSON object carrying
    ``image_base64`` — which is what the simulator's ``robot.camera.capture`` returns and
    what the firmware contract specifies (docs/mcp.md).
    """

    #: The tool a capture goes to, spelled the way the **server** keys it. The device
    #: publishes ``robot.camera.capture``; discovery sanitizes the dots away and every
    #: capability lookup and dispatch uses the sanitized name
    #: (``robot/actions/model.py``, ``CaptureImageAction.tool_name``). Spelled out rather
    #: than imported: ``robot/vision`` may not import the action layer, because a module
    #: that can see an action spec is one refactor away from submitting one.
    TOOL = "robot_camera_capture"

    def __init__(self, call_tool: Any, *, tool: str = TOOL, timeout_s: float = 5.0) -> None:
        self._call_tool = call_tool
        self.tool = tool
        self.timeout_s = timeout_s

    async def capture(self) -> Frame | None:
        raw = await self._call_tool(self.tool, {}, timeout=self.timeout_s)
        return self.decode_reply(raw)

    @staticmethod
    def decode_reply(raw: str) -> Frame | None:
        """A device reply as a frame, or ``None`` when it carries no image."""
        import base64
        import json

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        encoded = payload.get("image_base64")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            # validate=True, or base64 silently drops every character it does not
            # recognize and hands back empty bytes that look like a valid tiny frame.
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            logger.warning("vision: the device returned image data that is not base64")
            return None
        if not data:
            return None
        return Frame(
            data=data,
            media_type=str(payload.get("mime_type", "image/png")),
            width=int(payload.get("width", 0) or 0),
            height=int(payload.get("height", 0) or 0),
            source="device",
        )


class StaticFrameSource:
    """Serves a fixed list of frames, then repeats the last one. For fixtures and tests."""

    def __init__(self, frames: Sequence[Frame | bytes]) -> None:
        self.frames = [f if isinstance(f, Frame) else Frame(data=f) for f in frames]
        self.calls = 0

    async def capture(self) -> Frame | None:
        if not self.frames:
            return None
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return frame

    @classmethod
    def from_directory(cls, directory: str | Path) -> StaticFrameSource:
        """Every committed image in a fixtures directory, in name order."""
        root = Path(directory)
        paths = sorted(p for p in root.glob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"})
        return cls([Frame(data=path.read_bytes(), source=path.name) for path in paths])


__all__ = [
    "DEFAULT_INTERVAL_S",
    "CallableFrameSource",
    "McpFrameSource",
    "StaticFrameSource",
    "VisionPipeline",
]
