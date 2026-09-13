"""The room the simulated robot drives around in.

Deliberately 2D and deliberately small: walls, obstacles, cliffs, people, objects and a
charging dock, with a ray cast for the distance sensors and a point test for the cliff
sensors. It exists so the sensor readings a behaviour test sees are *consistent with each
other* — a front distance of 0.2 m and a completed 1 m move cannot both happen — not to
be a physics engine.

Units are metres and radians throughout; the wire vocabulary converts to integer
millimetres and degrees at the tool boundary (docs/robot-simulator.md).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Distance sensors report this when nothing is in range.
DEFAULT_MAX_RANGE_M = 2.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Wall(_Model):
    """A line segment the robot cannot drive through."""

    x1_m: float
    y1_m: float
    x2_m: float
    y2_m: float


class Obstacle(_Model):
    """A circular obstacle. ``removable`` ones are what scenarios add and take away."""

    id: str
    x_m: float
    y_m: float
    radius_m: float = 0.1


class Cliff(_Model):
    """An axis-aligned region where the floor is not there."""

    id: str
    x0_m: float
    y0_m: float
    x1_m: float
    y1_m: float

    def contains(self, x_m: float, y_m: float) -> bool:
        return min(self.x0_m, self.x1_m) <= x_m <= max(self.x0_m, self.x1_m) and min(
            self.y0_m, self.y1_m
        ) <= y_m <= max(self.y0_m, self.y1_m)


class Person(_Model):
    id: str
    x_m: float
    y_m: float
    name: str = "person"


class WorldObject(_Model):
    id: str
    x_m: float
    y_m: float
    label: str = "object"


class WorldSpec(_Model):
    """A room, as a scenario file writes it."""

    width_m: float = 4.0
    depth_m: float = 3.0
    walls: list[Wall] = Field(default_factory=list)
    obstacles: list[Obstacle] = Field(default_factory=list)
    cliffs: list[Cliff] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)
    objects: list[WorldObject] = Field(default_factory=list)
    dock_x_m: float | None = 0.2
    dock_y_m: float | None = 0.2
    max_range_m: float = DEFAULT_MAX_RANGE_M
    #: How close the robot has to be to the dock to charge.
    dock_radius_m: float = 0.15
    #: Half-angle of the camera's field of view, in degrees.
    camera_fov_deg: float = 60.0


class World:
    """A mutable room. Scenarios mutate it; sensors read it; nothing else touches it."""

    def __init__(self, spec: WorldSpec | None = None) -> None:
        self.spec = spec or default_room()
        self.walls: list[Wall] = list(self.spec.walls)
        self.obstacles: dict[str, Obstacle] = {o.id: o for o in self.spec.obstacles}
        self.cliffs: dict[str, Cliff] = {c.id: c for c in self.spec.cliffs}
        self.people: dict[str, Person] = {p.id: p for p in self.spec.people}
        self.objects: dict[str, WorldObject] = {o.id: o for o in self.spec.objects}

    # -- mutation, driven by the scenario runner ---------------------------------------

    def add_obstacle(self, obstacle: Obstacle) -> None:
        self.obstacles[obstacle.id] = obstacle

    def remove_obstacle(self, obstacle_id: str) -> bool:
        return self.obstacles.pop(obstacle_id, None) is not None

    def add_cliff(self, cliff: Cliff) -> None:
        self.cliffs[cliff.id] = cliff

    def remove_cliff(self, cliff_id: str) -> bool:
        return self.cliffs.pop(cliff_id, None) is not None

    def add_person(self, person: Person) -> None:
        self.people[person.id] = person

    def remove_person(self, person_id: str) -> bool:
        return self.people.pop(person_id, None) is not None

    # -- sensing ------------------------------------------------------------------------

    @property
    def max_range_m(self) -> float:
        return self.spec.max_range_m

    def distance(self, x_m: float, y_m: float, heading_rad: float) -> float:
        """Range to the nearest wall or obstacle along ``heading_rad``, capped at range."""
        dx, dy = math.cos(heading_rad), math.sin(heading_rad)
        best = self.max_range_m
        for wall in self.walls:
            hit = _ray_segment(x_m, y_m, dx, dy, wall)
            if hit is not None and hit < best:
                best = hit
        for obstacle in self.obstacles.values():
            hit = _ray_circle(x_m, y_m, dx, dy, obstacle.x_m, obstacle.y_m, obstacle.radius_m)
            if hit is not None and hit < best:
                best = hit
        return best

    def cliff_at(self, x_m: float, y_m: float) -> str | None:
        """The id of the cliff under a point, or ``None``."""
        for cliff in self.cliffs.values():
            if cliff.contains(x_m, y_m):
                return cliff.id
        return None

    def obstacle_at(self, x_m: float, y_m: float, *, clearance_m: float = 0.0) -> str | None:
        """The id of an obstacle a robot at this point would be inside, or ``None``."""
        for obstacle in self.obstacles.values():
            if math.hypot(x_m - obstacle.x_m, y_m - obstacle.y_m) <= obstacle.radius_m + clearance_m:
                return obstacle.id
        return None

    def crosses_wall(self, x0_m: float, y0_m: float, x1_m: float, y1_m: float) -> bool:
        """Whether the straight move from one point to the other passes through a wall."""
        dx, dy = x1_m - x0_m, y1_m - y0_m
        length = math.hypot(dx, dy)
        if length <= 0:
            return False
        for wall in self.walls:
            hit = _ray_segment(x0_m, y0_m, dx / length, dy / length, wall)
            if hit is not None and hit <= length:
                return True
        return False

    def at_dock(self, x_m: float, y_m: float) -> bool:
        if self.spec.dock_x_m is None or self.spec.dock_y_m is None:
            return False
        return math.hypot(x_m - self.spec.dock_x_m, y_m - self.spec.dock_y_m) <= self.spec.dock_radius_m

    def dock_bearing(self, x_m: float, y_m: float, heading_rad: float) -> tuple[float, float] | None:
        """``(distance_m, relative_bearing_rad)`` of the dock, or ``None`` if there is none."""
        if self.spec.dock_x_m is None or self.spec.dock_y_m is None:
            return None
        return _bearing(x_m, y_m, heading_rad, self.spec.dock_x_m, self.spec.dock_y_m)

    def visible(
        self, x_m: float, y_m: float, heading_rad: float, *, range_m: float | None = None
    ) -> list[dict[str, Any]]:
        """People and objects inside the camera's field of view, nearest first.

        Bearings are relative to where the robot is looking, so a caller can turn the list
        into a camera frame without knowing the world frame.
        """
        limit = range_m if range_m is not None else self.spec.max_range_m * 2
        half_fov = math.radians(self.spec.camera_fov_deg) / 2
        seen: list[dict[str, Any]] = []
        for kind, entity_id, ex, ey, label in self._entities():
            distance_m, bearing_rad = _bearing(x_m, y_m, heading_rad, ex, ey)
            if distance_m > limit or abs(bearing_rad) > half_fov:
                continue
            seen.append(
                {
                    "kind": kind,
                    "id": entity_id,
                    "label": label,
                    "distance_mm": int(round(distance_m * 1000)),
                    "bearing_deg": int(round(math.degrees(bearing_rad))),
                }
            )
        seen.sort(key=lambda entry: int(entry["distance_mm"]))
        return seen

    def _entities(self) -> list[tuple[str, str, float, float, str]]:
        entities: list[tuple[str, str, float, float, str]] = [
            ("person", person.id, person.x_m, person.y_m, person.name) for person in self.people.values()
        ]
        entities += [("object", obj.id, obj.x_m, obj.y_m, obj.label) for obj in self.objects.values()]
        if self.spec.dock_x_m is not None and self.spec.dock_y_m is not None:
            entities.append(("dock", "dock", self.spec.dock_x_m, self.spec.dock_y_m, "charging dock"))
        return entities

    def summary(self) -> dict[str, Any]:
        return {
            "width_m": self.spec.width_m,
            "depth_m": self.spec.depth_m,
            "walls": len(self.walls),
            "obstacles": sorted(self.obstacles),
            "cliffs": sorted(self.cliffs),
            "people": sorted(self.people),
            "objects": sorted(self.objects),
            "dock": None
            if self.spec.dock_x_m is None or self.spec.dock_y_m is None
            else [self.spec.dock_x_m, self.spec.dock_y_m],
        }


def default_room(width_m: float = 4.0, depth_m: float = 3.0) -> WorldSpec:
    """An empty rectangular room with a dock in the near-left corner."""
    return WorldSpec(
        width_m=width_m,
        depth_m=depth_m,
        walls=[
            Wall(x1_m=0.0, y1_m=0.0, x2_m=width_m, y2_m=0.0),
            Wall(x1_m=width_m, y1_m=0.0, x2_m=width_m, y2_m=depth_m),
            Wall(x1_m=width_m, y1_m=depth_m, x2_m=0.0, y2_m=depth_m),
            Wall(x1_m=0.0, y1_m=depth_m, x2_m=0.0, y2_m=0.0),
        ],
    )


def _bearing(x_m: float, y_m: float, heading_rad: float, tx_m: float, ty_m: float) -> tuple[float, float]:
    dx, dy = tx_m - x_m, ty_m - y_m
    distance_m = math.hypot(dx, dy)
    bearing = _wrap(math.atan2(dy, dx) - heading_rad)
    return distance_m, bearing


def _wrap(angle_rad: float) -> float:
    """The same angle in ``(-pi, pi]``."""
    return (angle_rad + math.pi) % (2 * math.pi) - math.pi


def _ray_segment(x: float, y: float, dx: float, dy: float, wall: Wall) -> float | None:
    """Distance along a unit ray to a segment, or ``None`` when it misses."""
    sx, sy = wall.x2_m - wall.x1_m, wall.y2_m - wall.y1_m
    denominator = dx * sy - dy * sx
    if abs(denominator) < 1e-12:  # parallel
        return None
    wx, wy = wall.x1_m - x, wall.y1_m - y
    t = (wx * sy - wy * sx) / denominator  # along the ray
    u = (wx * dy - wy * dx) / denominator  # along the segment
    if t < 0 or not 0.0 <= u <= 1.0:
        return None
    return t


def _ray_circle(x: float, y: float, dx: float, dy: float, cx: float, cy: float, radius: float) -> float | None:
    """Distance along a unit ray to a circle's near side, or ``None`` when it misses."""
    ox, oy = x - cx, y - cy
    b = ox * dx + oy * dy
    c = ox * ox + oy * oy - radius * radius
    discriminant = b * b - c
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    for candidate in (-b - root, -b + root):
        if candidate >= 0:
            return candidate
    return None
