"""Robot vision: snapshots in, tracked entities and events out.

    from robot.vision import VisionPipeline, FakeDetector

    pipeline = VisionPipeline("nilo-sim-01", source, world=runtime.world, events=runtime.events)
    result = await pipeline.process()        # one frame, all the way to the world model

Modular by construction. Five roles — frame source, provider, object detector, face
detector, face recognizer — plus a tracker, each behind its own protocol, each replaceable
without the others knowing. The defaults need **nothing installed**: a header-reading
provider, a detector that finds nothing, and a registry that matches embeddings with
arithmetic. OpenCV and Ultralytics are used when present and imported lazily, so a machine
with neither still imports this package for free (docs/robot-vision.md).

Snapshots, not video. One call gives one frame; continuous perception is a timer over that
call, never a requirement.

Layering (docs/robot-architecture.md Sect. 7): this package may import ``robot/state`` and
``robot/events``. It may **not** import ``robot/actions`` or ``robot/behavior`` — vision
writes the world model and publishes events; deciding what to do about a person, and
commanding the robot to do it, happens one layer up.
"""

from robot.vision.faces import (
    DEFAULT_MATCH_THRESHOLD,
    EmbeddingFaceRecognizer,
    FaceRecord,
    FaceRegistry,
    cosine_similarity,
)
from robot.vision.pipeline import (
    DEFAULT_INTERVAL_S,
    CallableFrameSource,
    McpFrameSource,
    StaticFrameSource,
    VisionPipeline,
)
from robot.vision.providers import (
    FaceDetector,
    FaceRecognizer,
    FakeDetector,
    FrameSource,
    HeaderVisionProvider,
    NullDetector,
    ObjectDetector,
    OpenCVVisionProvider,
    VisionProvider,
    VisionUnavailable,
    YoloObjectDetector,
)
from robot.vision.tracker import (
    DEFAULT_MAX_MISSES,
    CentroidTracker,
    Track,
    Tracker,
)
from robot.vision.types import (
    BoundingBox,
    Detection,
    DetectionKind,
    FaceIdentity,
    Frame,
    FrameRetention,
    VisionMetrics,
    VisionResult,
    position_from_box,
)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_MAX_MISSES",
    "BoundingBox",
    "CallableFrameSource",
    "CentroidTracker",
    "Detection",
    "DetectionKind",
    "EmbeddingFaceRecognizer",
    "FaceDetector",
    "FaceIdentity",
    "FaceRecognizer",
    "FaceRecord",
    "FaceRegistry",
    "FakeDetector",
    "Frame",
    "FrameRetention",
    "FrameSource",
    "HeaderVisionProvider",
    "McpFrameSource",
    "NullDetector",
    "ObjectDetector",
    "OpenCVVisionProvider",
    "StaticFrameSource",
    "Track",
    "Tracker",
    "VisionMetrics",
    "VisionPipeline",
    "VisionProvider",
    "VisionResult",
    "VisionUnavailable",
    "YoloObjectDetector",
    "cosine_similarity",
    "position_from_box",
]
