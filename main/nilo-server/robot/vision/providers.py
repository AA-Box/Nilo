"""The provider interfaces, and the implementations that need nothing installed.

Five roles, five protocols:

``FrameSource``     where an image comes from (a device tool, a file, a fixture)
``VisionProvider``  decode and preprocess one frame
``ObjectDetector``  boxes for things
``FaceDetector``    boxes for faces
``FaceRecognizer``  a name for a face box

They are separate because they fail, scale and get replaced separately: a deployment can
run YOLO for objects, nothing for faces, and a fixture source for frames, without any of
those three knowing about the others.

**Nothing heavyweight is required.** The defaults here are pure Python: a PNG/JPEG header
reader for dimensions, a no-op preprocessor, and a detector that finds nothing. OpenCV and
Ultralytics are used *if they are installed* and are never imported at module scope, so
`import robot.vision` costs nothing on a machine that has neither (docs/robot-vision.md).
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from robot.vision.types import BoundingBox, Detection, DetectionKind, FaceIdentity, Frame

logger = logging.getLogger(__name__)


class VisionUnavailable(RuntimeError):
    """A provider was asked for something the machine cannot do (no model, no library)."""


@runtime_checkable
class FrameSource(Protocol):
    """Where a frame comes from. One call, one image, no streaming.

    Snapshots rather than video on purpose: the device already has a capture tool, a
    still image is what a vision endpoint accepts, and a pipeline that needs continuous
    frames cannot run on hardware that cannot send them (docs/robot-vision.md).
    """

    async def capture(self) -> Frame | None: ...


@runtime_checkable
class VisionProvider(Protocol):
    """Decode and preprocess. The half of vision that is image handling rather than models."""

    @property
    def name(self) -> str: ...

    def decode(self, frame: Frame) -> Frame:
        """Fill in width and height (and anything else cheap) without changing the image."""

    def preprocess(self, frame: Frame) -> Frame:
        """Whatever the detectors want done first: resize, colour conversion, nothing."""


@runtime_checkable
class ObjectDetector(Protocol):
    """Finds things. Returns normalized boxes, never pixels."""

    @property
    def name(self) -> str: ...

    async def detect(self, frame: Frame) -> Sequence[Detection]: ...


@runtime_checkable
class FaceDetector(Protocol):
    """Finds faces. Separate from :class:`ObjectDetector` because it usually is."""

    @property
    def name(self) -> str: ...

    async def detect_faces(self, frame: Frame) -> Sequence[Detection]: ...


@runtime_checkable
class FaceRecognizer(Protocol):
    """Puts a name to a face box, or says it does not know."""

    @property
    def name(self) -> str: ...

    async def recognize(self, frame: Frame, detection: Detection) -> FaceIdentity | None: ...


# -- the implementations that need nothing installed --------------------------------------------


class NullDetector:
    """Finds nothing, successfully.

    The default, and the right answer for a deployment with no model: the pipeline runs,
    the world model stays empty, and every behaviour that needs a person simply does not
    fire. A robot with no detector should be boring, not broken.
    """

    name = "null"

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        return ()

    async def detect_faces(self, frame: Frame) -> Sequence[Detection]:
        return ()


class FakeDetector:
    """Returns a script. What every vision test in this repository runs against.

    ``frames`` is a list of detection lists: the first call returns the first entry, the
    second the second, and the last entry repeats forever. ``fail_on`` makes a given call
    raise, which is how provider failure is tested without breaking a real one.
    """

    name = "fake"

    def __init__(
        self,
        frames: Iterable[Sequence[Detection]] = (),
        *,
        fail_on: Iterable[int] = (),
        error: Exception | None = None,
    ) -> None:
        self.frames: list[Sequence[Detection]] = [tuple(entry) for entry in frames]
        self.fail_on = set(fail_on)
        self.error = error or VisionUnavailable("the detector failed")
        self.calls = 0

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        index = self.calls
        self.calls += 1
        if index in self.fail_on:
            raise self.error
        if not self.frames:
            return ()
        return self.frames[min(index, len(self.frames) - 1)]

    async def detect_faces(self, frame: Frame) -> Sequence[Detection]:
        return tuple(d for d in await self.detect(frame) if d.kind is DetectionKind.FACE)

    def script(self, *frames: Sequence[Detection]) -> FakeDetector:
        """Replace the script. Returns self, so it reads well in a test."""
        self.frames = [tuple(entry) for entry in frames]
        self.calls = 0
        return self


#: What the simulator's camera paints each kind of thing, from ``robot/simulator/camera.py``.
#: A detector that reads these is a detector with no model in it and no randomness, which
#: is the only kind that belongs in CI.
SCENE_COLOURS: dict[tuple[int, int, int], tuple[DetectionKind, str]] = {
    (226, 122, 96): (DetectionKind.PERSON, "person"),
    (108, 176, 132): (DetectionKind.OBJECT, "object"),
    (96, 132, 226): (DetectionKind.OBJECT, "dock"),
}

#: Ignore a blob smaller than this fraction of the frame. The renderer's smallest box is
#: 3x4 pixels in a 160x120 frame, which is above this; single stray pixels are not.
MIN_BLOB_AREA = 0.0008


class ColourBlobDetector:
    """Finds the simulator's flat-coloured boxes by reading the pixels. No model, no library.

    This is the deterministic local detector the end-to-end suite runs against. It is a
    real detector in the only sense that matters for an integration test: it is handed the
    bytes the device actually sent, and what it finds is a function of where the scenario
    put a person — not of a script a test wrote next to the assertion.

    It only understands what ``robot/simulator/camera.py`` produces: an 8-bit RGB PNG whose
    scanlines all use filter type 0. That is not a limitation worth removing — a frame from
    a real camera is a photograph, and the answer for a photograph is a model, not this.
    Anything it cannot read yields no detections rather than an error, which is the same
    contract :class:`NullDetector` has.
    """

    name = "colour-blob"

    def __init__(
        self,
        colours: dict[tuple[int, int, int], tuple[DetectionKind, str]] | None = None,
        *,
        min_area: float = MIN_BLOB_AREA,
    ) -> None:
        self.colours = dict(SCENE_COLOURS if colours is None else colours)
        self.min_area = min_area

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        if not frame.data:
            return ()
        decoded = _decode_png(frame.data)
        if decoded is None:
            return ()
        width, height, rows = decoded
        found: list[Detection] = []
        for colour, (kind, label) in self.colours.items():
            for box in _blobs(rows, width, height, colour):
                if box.area < self.min_area:
                    continue
                found.append(Detection(kind=kind, box=box, confidence=1.0, label=label))
        # Largest first: the nearest thing is the one a behaviour should reason about, and
        # a stable order makes a failing assertion readable.
        found.sort(key=lambda detection: -detection.box.area)
        return tuple(found)

    async def detect_faces(self, frame: Frame) -> Sequence[Detection]:
        """The renderer draws no faces; a body is not a face and must not be reported as one."""
        return ()


def _decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]] | None:
    """8-bit RGB, filter 0 only. ``None`` for anything else — see :class:`ColourBlobDetector`."""
    import zlib  # noqa: PLC0415 - stdlib, but only this one function needs it

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset, header, idat = 8, None, bytearray()
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat += payload
        elif kind == b"IEND":
            break
        offset += 12 + length
    if header is None:
        return None
    width, height, depth, colour_type, _, _, interlace = header
    if depth != 8 or colour_type != 2 or interlace != 0:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    stride = width * 3
    if len(raw) < height * (stride + 1):
        return None
    rows: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        start = y * (stride + 1)
        if raw[start] != 0:  # a filtered scanline: not something this decoder claims to read
            return None
        line = raw[start + 1 : start + 1 + stride]
        rows.append([(line[x * 3], line[x * 3 + 1], line[x * 3 + 2]) for x in range(width)])
    return width, height, rows


def _blobs(
    rows: list[list[tuple[int, int, int]]], width: int, height: int, colour: tuple[int, int, int]
) -> list[BoundingBox]:
    """Connected regions of one exact colour, as normalized boxes.

    A flood fill rather than a bounding box over every matching pixel: two people in the
    frame are two detections, and one box around both of them is the bug that would make
    a greeting behaviour greet the space between them.
    """
    seen = [[False] * width for _ in range(height)]
    boxes: list[BoundingBox] = []
    for y in range(height):
        for x in range(width):
            if seen[y][x] or rows[y][x] != colour:
                continue
            left = right = x
            top = bottom = y
            stack = [(x, y)]
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                left, right = min(left, cx), max(right, cx)
                top, bottom = min(top, cy), max(bottom, cy)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx] and rows[ny][nx] == colour:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            boxes.append(
                BoundingBox.from_pixels(
                    left,
                    top,
                    right - left + 1,
                    bottom - top + 1,
                    frame_width=width,
                    frame_height=height,
                )
            )
    return boxes


class HeaderVisionProvider:
    """Reads the image dimensions out of the file header. No dependencies at all.

    PNG and JPEG cover what the simulator renders and what every camera the project has
    met so far produces. A format it cannot read leaves the dimensions at zero, which the
    pipeline treats as "unknown" rather than as an error — normalized coordinates do not
    need the resolution, so nothing downstream actually breaks.
    """

    name = "header"

    def decode(self, frame: Frame) -> Frame:
        if frame.width and frame.height:
            return frame
        if not frame.data:
            return frame
        size = _png_size(frame.data) or _jpeg_size(frame.data)
        if size is None:
            return frame
        width, height = size
        return frame.model_copy(update={"width": width, "height": height})

    def preprocess(self, frame: Frame) -> Frame:
        return frame


class OpenCVVisionProvider:
    """Decode and preprocess with OpenCV, when it is installed.

    Imported lazily and only on first use: ``import cv2`` is expensive and is not in the
    test dependency slice, so a machine without it must never pay for the import. Without
    OpenCV this falls back to :class:`HeaderVisionProvider`, which is enough for
    normalized coordinates, and says so once in the log.
    """

    name = "opencv"

    def __init__(self, *, max_width: int = 640, grayscale: bool = False) -> None:
        self.max_width = max_width
        self.grayscale = grayscale
        self._cv2: Any | None = None
        self._checked = False
        self._fallback = HeaderVisionProvider()

    @property
    def available(self) -> bool:
        return self._load() is not None

    def decode(self, frame: Frame) -> Frame:
        cv2 = self._load()
        if cv2 is None or not frame.data:
            return self._fallback.decode(frame)
        image = self._to_array(cv2, frame)
        if image is None:
            return self._fallback.decode(frame)
        height, width = image.shape[:2]
        return frame.model_copy(update={"width": int(width), "height": int(height)})

    def preprocess(self, frame: Frame) -> Frame:
        """Downscale wide frames. A detector does not get better above 640 px, and every
        pixel above it is latency on a robot's CPU."""
        cv2 = self._load()
        if cv2 is None or not frame.data or frame.width <= self.max_width:
            return frame
        image = self._to_array(cv2, frame)
        if image is None:
            return frame
        scale = self.max_width / float(frame.width)
        resized = cv2.resize(image, (self.max_width, max(1, int(frame.height * scale))))
        if self.grayscale:
            resized = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        ok, encoded = cv2.imencode(".png", resized)
        if not ok:  # pragma: no cover - imencode failing means a broken OpenCV build
            return frame
        height, width = resized.shape[:2]
        return frame.model_copy(
            update={"data": encoded.tobytes(), "width": int(width), "height": int(height)}
        )

    def _load(self) -> Any | None:
        if not self._checked:
            self._checked = True
            try:
                import cv2  # noqa: PLC0415 - deliberately lazy: see the class docstring

                self._cv2 = cv2
            except ImportError:
                logger.info("OpenCV is not installed; vision falls back to header decoding")
                self._cv2 = None
        return self._cv2

    @staticmethod
    def _to_array(cv2: Any, frame: Frame) -> Any | None:
        import numpy as np  # noqa: PLC0415 - only reachable when OpenCV imported

        buffer = np.frombuffer(frame.data or b"", dtype=np.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        return image if image is not None else None


class YoloObjectDetector:
    """Ultralytics YOLO, when it is installed and a model is present.

    Optional in the strongest sense: the import is lazy, the model path is configurable,
    and a missing package or weight file raises :class:`VisionUnavailable` at
    construction-check time rather than at the first frame. The pipeline catches that and
    keeps running with whatever else it has — a robot must not lose perception entirely
    because a model file moved.
    """

    name = "yolo"

    #: Class names worth turning into world entities. A robot does not need to track
    #: every one of eighty COCO classes, and an entity per traffic light is noise.
    DEFAULT_CLASSES: tuple[str, ...] = (
        "person",
        "cup",
        "bottle",
        "book",
        "cell phone",
        "laptop",
        "chair",
        "sports ball",
        "teddy bear",
        "remote",
    )

    def __init__(
        self,
        model: str = "yolo11n.pt",
        *,
        confidence: float = 0.35,
        classes: Sequence[str] | None = None,
    ) -> None:
        self.model_name = model
        self.confidence = confidence
        self.classes = tuple(classes) if classes is not None else self.DEFAULT_CLASSES
        self._model: Any | None = None

    def load(self) -> Any:
        """Load the model, raising :class:`VisionUnavailable` if that is not possible."""
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO  # noqa: PLC0415 - heavyweight and optional
        except ImportError as exc:
            raise VisionUnavailable("ultralytics is not installed") from exc
        try:
            self._model = YOLO(self.model_name)
        except Exception as exc:  # a missing weight file, a corrupt download
            raise VisionUnavailable(f"could not load {self.model_name}: {exc}") from exc
        return self._model

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        """Run the model. Blocking work goes to a thread — never on the session loop."""
        if not frame.data:
            return ()
        import asyncio  # noqa: PLC0415 - local, so the module imports without an event loop

        model = self.load()
        return await asyncio.to_thread(self._predict, model, frame)

    def _predict(self, model: Any, frame: Frame) -> Sequence[Detection]:
        import numpy as np  # noqa: PLC0415
        buffer = np.frombuffer(frame.data or b"", dtype=np.uint8)
        results = model.predict(source=buffer, verbose=False, conf=self.confidence)
        detections: list[Detection] = []
        for result in results:
            names = getattr(result, "names", {})
            height, width = getattr(result, "orig_shape", (frame.height, frame.width))
            for box in getattr(result, "boxes", []):
                label = str(names.get(int(box.cls), "object"))
                if self.classes and label not in self.classes:
                    continue
                x1, y1, x2, y2 = (float(value) for value in box.xyxy[0])
                detections.append(
                    Detection(
                        kind=DetectionKind.PERSON if label == "person" else DetectionKind.OBJECT,
                        box=BoundingBox.from_pixels(
                            x1, y1, x2 - x1, y2 - y1, frame_width=int(width), frame_height=int(height)
                        ),
                        confidence=float(box.conf),
                        label=label,
                        attributes={"detector": self.name},
                    )
                )
        return tuple(detections)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG markers to the first start-of-frame. Enough for dimensions."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        if index + 4 > len(data):
            return None
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + length
    return None


__all__ = [
    "MIN_BLOB_AREA",
    "SCENE_COLOURS",
    "ColourBlobDetector",
    "FaceDetector",
    "FaceRecognizer",
    "FakeDetector",
    "FrameSource",
    "HeaderVisionProvider",
    "NullDetector",
    "ObjectDetector",
    "OpenCVVisionProvider",
    "VisionProvider",
    "VisionUnavailable",
    "YoloObjectDetector",
]
