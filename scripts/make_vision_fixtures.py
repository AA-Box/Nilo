#!/usr/bin/env python3
"""Render the committed vision fixtures from the simulator's own camera.

    cd main/nilo-server && PYTHONPATH=. python ../../scripts/make_vision_fixtures.py

The output lands in ``main/nilo-server/tests/robot/fixtures/vision/`` and is committed.
Eight small PNGs: an empty room, a person left/centre/right/close, a person with an
object, an object alone, and the dock. They are a few hundred bytes each, contain no
photograph of anybody, and let the vision tests assert on real image bytes with no camera
and no network (docs/robot-vision.md).

Regenerate after changing the simulator's renderer, and commit what changes.
"""
from pathlib import Path

from robot.simulator.camera import render_frame

OUT = Path("tests/robot/fixtures/vision")

SCENES = {
    # name: (visible entities, cliff)
    "01_empty_room": ([], False),
    "02_person_left": ([{"kind": "person", "id": "p1", "label": "person", "distance_mm": 1600, "bearing_deg": 20}], False),
    "03_person_centre": ([{"kind": "person", "id": "p1", "label": "person", "distance_mm": 1600, "bearing_deg": 0}], False),
    "04_person_right": ([{"kind": "person", "id": "p1", "label": "person", "distance_mm": 1600, "bearing_deg": -20}], False),
    "05_person_close": ([{"kind": "person", "id": "p1", "label": "person", "distance_mm": 700, "bearing_deg": 0}], False),
    "06_person_and_object": (
        [
            {"kind": "person", "id": "p1", "label": "person", "distance_mm": 1500, "bearing_deg": 12},
            {"kind": "object", "id": "o1", "label": "cube", "distance_mm": 900, "bearing_deg": -18},
        ],
        False,
    ),
    "07_object_only": ([{"kind": "object", "id": "o1", "label": "cube", "distance_mm": 900, "bearing_deg": -18}], False),
    "08_dock_ahead": ([{"kind": "dock", "id": "d1", "label": "dock", "distance_mm": 2200, "bearing_deg": 0}], False),
}

OUT.mkdir(parents=True, exist_ok=True)
for name, (visible, cliff) in SCENES.items():
    png = render_frame(visible, cliff=cliff)
    (OUT / f"{name}.png").write_bytes(png)
    print(name, len(png), "bytes")
