"""The simulated camera: a synthetic frame, or a frame from a fixture directory.

The frame is a real PNG, written by hand with ``zlib`` and ``struct`` rather than by a
new imaging dependency, because the only thing that has to be true of it is that the
backend accepts it: ``core/utils/util.py:is_valid_image_file`` sniffs the PNG magic
number and ``core/api/vision_handler.py`` base64-encodes whatever it is given.

What it draws is derived from the world, not decorative: each person, object and dock in
the field of view becomes a box whose horizontal position comes from its bearing and
whose height comes from its distance. A vision test that says "the person is on the left"
can therefore assert on pixels, and a scenario that moves a person changes the frame.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from itertools import cycle
from pathlib import Path
from typing import Any

DEFAULT_WIDTH = 160
DEFAULT_HEIGHT = 120

#: Files a fixture directory may contain. The backend's sniffer accepts all of them.
FIXTURE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})

_SKY = (118, 152, 184)
_FLOOR_NEAR = (96, 88, 80)
_FLOOR_FAR = (152, 144, 136)
_COLOURS: dict[str, tuple[int, int, int]] = {
    "person": (226, 122, 96),
    "object": (108, 176, 132),
    "dock": (96, 132, 226),
}
_CLIFF = (24, 24, 28)


class CameraFailure(RuntimeError):
    """The camera did not produce a frame. Injected, or a fixture that will not read."""


def encode_png(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    """A minimal 8-bit RGB PNG. No filtering, one IDAT, stdlib only."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None) for this scanline
        for red, green, blue in row:
            raw += bytes((red, green, blue))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def render_frame(
    visible: list[dict[str, Any]],
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fov_deg: float = 60.0,
    max_range_mm: int = 4000,
    cliff: bool = False,
) -> bytes:
    """A PNG of what the robot is looking at, from :meth:`World.visible` output."""
    horizon = int(height * 0.45)
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(height):
        if y < horizon:
            pixels.append([_SKY] * width)
            continue
        depth = (y - horizon) / max(1, height - horizon)
        pixels.append([_blend(_FLOOR_FAR, _FLOOR_NEAR, depth)] * width)

    for entry in sorted(visible, key=lambda item: -int(item["distance_mm"])):
        _draw_box(
            pixels, entry, width=width, height=height, horizon=horizon, fov_deg=fov_deg, max_range_mm=max_range_mm
        )

    if cliff:
        for y in range(max(0, height - 10), height):
            pixels[y] = [_CLIFF] * width
    return encode_png(width, height, pixels)


def _draw_box(
    pixels: list[list[tuple[int, int, int]]],
    entry: dict[str, Any],
    *,
    width: int,
    height: int,
    horizon: int,
    fov_deg: float,
    max_range_mm: int,
) -> None:
    distance_mm = max(1, int(entry["distance_mm"]))
    bearing_deg = float(entry["bearing_deg"])
    # Bearing is positive to the robot's left, which is the left of the image.
    centre = int(width / 2 - (bearing_deg / (fov_deg / 2)) * (width / 2))
    scale = min(1.0, max_range_mm / (distance_mm * 4))
    box_h = max(4, int((height - horizon) * scale))
    box_w = max(3, int(box_h * 0.55))
    colour = _COLOURS.get(str(entry["kind"]), _COLOURS["object"])
    top = max(0, horizon - box_h // 3)
    bottom = min(height, top + box_h)
    for y in range(top, bottom):
        row = pixels[y]
        for x in range(max(0, centre - box_w // 2), min(width, centre + box_w // 2 + 1)):
            row[x] = colour


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


class FixtureCamera:
    """Serves committed image files in a stable order, looping.

    For tests that need a real photograph: the synthetic renderer cannot produce one, and
    a vision model asked about a coloured box gives an answer nobody can assert on.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        files = sorted(p for p in self.directory.glob("*") if p.suffix.lower() in FIXTURE_SUFFIXES)
        if not files:
            raise CameraFailure(f"no fixture images in {self.directory}")
        self.files = files
        self._cycle: Iterator[Path] = cycle(files)

    def capture(self) -> tuple[bytes, str]:
        path = next(self._cycle)
        try:
            return path.read_bytes(), path.name
        except OSError as exc:  # a fixture that vanished mid-run
            raise CameraFailure(f"cannot read fixture {path}: {exc}") from exc
