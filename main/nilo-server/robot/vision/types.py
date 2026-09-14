"""The vocabulary of robot vision: frames, detections, identities and latency.

Two rules run through every model here.

* **Coordinates are normalized.** A :class:`BoundingBox` is in 0.0–1.0 image space, never
  pixels. A tracker written against a 160x120 simulated frame keeps working on a 1600x1200
  one, "the person is on the left" is a comparison against 0.5, and nothing downstream has
  to know a resolution nobody recorded (docs/robot-architecture.md Sect. 2.4).
* **Frames are ephemeral by default.** A :class:`Frame` carries bytes, and those bytes are
  a camera image of somebody's living room. :class:`FrameRetention` decides whether they
  are kept at all, and the default is not to keep them (docs/robot-vision.md).

Nothing in this module imports a detector, a model or an image library. It is the shape
the rest of the subsystem speaks, and it is importable in a process with no OpenCV, no
numpy and no camera.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.state.models import utcnow
from robot.state.world import EntityType, ImagePoint, Position


class DetectionKind(str, Enum):
    """What a detector says it found. Deliberately small and mapped onto world entities."""

    PERSON = "person"
    FACE = "face"
    OBJECT = "object"

    @property
    def entity_type(self) -> EntityType:
        return {
            DetectionKind.PERSON: EntityType.PERSON,
            DetectionKind.FACE: EntityType.FACE,
            DetectionKind.OBJECT: EntityType.OBJECT,
        }[self]


class BoundingBox(BaseModel):
    """A box in normalized image space. ``0,0`` top-left, ``1,1`` bottom-right."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(ge=0.0, le=1.0)
    height: float = Field(ge=0.0, le=1.0)

    @classmethod
    def from_pixels(cls, x: float, y: float, width: float, height: float, *, frame_width: int, frame_height: int) -> BoundingBox:
        """Normalize a detector's pixel box. The one place pixels are allowed to appear."""
        if frame_width <= 0 or frame_height <= 0:
            raise ValueError("frame dimensions must be positive")
        return cls(
            x=_unit(x / frame_width),
            y=_unit(y / frame_height),
            width=_unit(width / frame_width),
            height=_unit(height / frame_height),
        )

    @property
    def centre(self) -> tuple[float, float]:
        return (_unit(self.x + self.width / 2), _unit(self.y + self.height / 2))

    @property
    def area(self) -> float:
        """Fraction of the frame covered. The only depth cue a single camera gives."""
        return self.width * self.height

    def to_image_point(self) -> ImagePoint:
        centre_x, centre_y = self.centre
        return ImagePoint(x=centre_x, y=centre_y, width=self.width, height=self.height)

    def iou(self, other: BoundingBox) -> float:
        """Intersection over union. What the tracker associates on."""
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.x + self.width, other.x + other.width)
        bottom = min(self.y + self.height, other.y + other.height)
        if right <= left or bottom <= top:
            return 0.0
        overlap = (right - left) * (bottom - top)
        union = self.area + other.area - overlap
        return overlap / union if union > 0 else 0.0

    def distance_to(self, other: BoundingBox) -> float:
        """Centre-to-centre distance, for associating boxes that do not overlap."""
        ax, ay = self.centre
        bx, by = other.centre
        return float(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)


class FaceIdentity(BaseModel):
    """Who a face belongs to, as far as recognition can tell.

    ``embedding_ref`` is a **reference**, not an embedding: the vector lives in the face
    registry (or, later, a memory store), and what travels through events and into the
    world model is an id. That keeps biometric data in one place with one lifetime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str
    display_name: str = ""
    embedding_ref: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def known(self) -> bool:
        """Whether this is somebody the registry has seen before."""
        return self.embedding_ref is not None

    @property
    def name(self) -> str:
        return self.display_name or self.person_id


class Detection(BaseModel):
    """One thing a detector found in one frame.

    Not yet a track: it has no id that survives the next frame. The tracker turns a
    detection into a track, and only a track becomes a world entity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DetectionKind
    box: BoundingBox
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    label: str = ""
    identity: FaceIdentity | None = None
    #: Whatever the provider wants to carry through: a class index, a landmark set, a
    #: model name. Never interpreted by the pipeline.
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def image_point(self) -> ImagePoint:
        return self.box.to_image_point()

    def describe(self) -> str:
        name = self.label or self.kind.value
        return f"{name} at {self.box.centre[0]:.2f},{self.box.centre[1]:.2f} ({self.confidence:.2f})"


class FrameRetention(str, Enum):
    """What happens to the image bytes once a frame has been processed.

    The default is :attr:`EPHEMERAL` — the bytes are dropped the moment detection is
    finished. A camera frame is a photograph of somebody's home; keeping one has to be a
    decision somebody made, not a default they inherited.
    """

    #: Drop the bytes as soon as the frame is processed. The default.
    EPHEMERAL = "ephemeral"
    #: Keep the most recent frame in memory only, for debugging. Never written to disk.
    LAST_ONLY = "last_only"
    #: Write frames to a directory. Only ever an explicit operator choice.
    PERSIST = "persist"


class Frame(BaseModel):
    """One camera image, plus what is known about it.

    ``data`` is the encoded image (PNG, JPEG — whatever the camera produced). It is
    dropped by :meth:`without_data` as soon as the pipeline is done with it, unless
    retention says otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: bytes | None = None
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    media_type: str = "image/png"
    captured_at: datetime = Field(default_factory=utcnow)
    source: str = "camera"

    @property
    def size_bytes(self) -> int:
        return len(self.data) if self.data else 0

    @property
    def digest(self) -> str:
        """A short content hash. Lets a log or a test name a frame without keeping it."""
        if not self.data:
            return ""
        return hashlib.sha256(self.data).hexdigest()[:16]

    def without_data(self) -> Frame:
        """The same frame with the image bytes dropped. What retention does."""
        if self.data is None:
            return self
        return self.model_copy(update={"data": None})


class VisionResult(BaseModel):
    """Everything one pass of the pipeline produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot_id: str
    detections: tuple[Detection, ...] = ()
    frame: Frame | None = None
    latency_ms: float = 0.0
    provider: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def of_kind(self, kind: DetectionKind) -> tuple[Detection, ...]:
        return tuple(detection for detection in self.detections if detection.kind is kind)


class VisionMetrics(BaseModel):
    """Processing latency and failure counts, per robot.

    Deliberately a few numbers rather than a histogram library: what an operator needs to
    know is "is perception keeping up, and is it failing", and both of those are visible
    here without adding a dependency.
    """

    model_config = ConfigDict(extra="forbid")

    frames: int = 0
    detections: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    last_latency_ms: float = 0.0

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.frames if self.frames else 0.0

    def record(self, latency_ms: float, detections: int = 0, *, failed: bool = False) -> None:
        self.frames += 1
        self.detections += detections
        self.total_latency_ms += latency_ms
        self.last_latency_ms = latency_ms
        self.max_latency_ms = max(self.max_latency_ms, latency_ms)
        if failed:
            self.failures += 1

    def snapshot(self) -> dict[str, float]:
        """Plain numbers, for a metrics endpoint or a log line."""
        return {
            "frames": float(self.frames),
            "detections": float(self.detections),
            "failures": float(self.failures),
            "mean_latency_ms": round(self.mean_latency_ms, 3),
            "max_latency_ms": round(self.max_latency_ms, 3),
            "last_latency_ms": round(self.last_latency_ms, 3),
        }


def position_from_box(box: BoundingBox, *, reference_area: float = 0.25, reference_mm: int = 1500, fov_deg: float = 60.0) -> Position:
    """A rough metric position from a normalized box. Honestly rough, and documented as such.

    One camera cannot measure distance. What it can do is compare the apparent size of a
    thing to how big that kind of thing usually looks at a known distance, which is what
    this does: ``reference_area`` of the frame means ``reference_mm`` away, and it scales
    with the inverse square root of the area. Bearing comes from how far off-centre the
    box is, across the field of view, and that part is trustworthy.

    Callers that need real distance use the range sensors; this exists so a behaviour can
    say "further than 900 mm" without a depth camera.
    """
    centre_x, _ = box.centre
    bearing = (0.5 - centre_x) * fov_deg
    area = max(box.area, 1e-6)
    distance = reference_mm * (reference_area / area) ** 0.5
    return Position(distance_mm=int(max(0, min(10_000, distance))), bearing_deg=round(bearing, 2))


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


__all__ = [
    "BoundingBox",
    "Detection",
    "DetectionKind",
    "FaceIdentity",
    "Frame",
    "FrameRetention",
    "VisionMetrics",
    "VisionResult",
    "position_from_box",
]
