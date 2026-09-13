"""The simulated robot's own state, and the step that moves it through time.

This is the authoritative copy. The backend's :class:`~robot.state.models.RobotState` is
a cache of what the robot last reported (docs/robot-architecture.md Sect. 2.4), so
everything here is in robot units and the notification layer converts.

Two properties the rest of the simulator depends on:

* **Motion takes time.** ``move 1000 mm`` at 200 mm/s occupies five seconds of simulated
  time, during which the pose changes every tick and the robot reports ``moving``. A tool
  call returns an action id immediately; completion arrives as a notification.
* **A motion can end badly.** An obstacle, a cliff, a flat battery, an injected motor
  failure or a cancel all end it, each with a distinct reason, and each produces
  ``notifications/motion_failed`` rather than a silent stop.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from robot.simulator.world import World, _bearing, _wrap

#: Robot geometry and rates. Every one of these is a calibration knob on real hardware
#: (docs/robot-roadmap.md Phase 8), so they are fields on a config object, not constants.
DEFAULT_MAX_SPEED_MMPS = 400
DEFAULT_MAX_TURN_DPS = 180


class MotionKind(str, Enum):
    MOVE = "move"
    TURN = "turn"
    #: Closed-loop tracking of a person in the world. Firmware's job on real hardware,
    #: which is why the backend names a target and a deadline rather than steering.
    FOLLOW = "follow"


class MotionOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    OBSTACLE = "obstacle"
    CLIFF = "cliff"
    MOTOR_FAILURE = "motor_failure"
    BATTERY_EMPTY = "battery_empty"
    SUPERSEDED = "superseded"
    TARGET_LOST = "target_lost"


@dataclass(frozen=True, slots=True)
class MotionResult:
    """How one motion ended. Turned into a notification by the device layer."""

    action_id: str
    kind: MotionKind
    outcome: MotionOutcome
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome is MotionOutcome.COMPLETED


@dataclass(slots=True)
class _Motion:
    action_id: str
    kind: MotionKind
    remaining: float  # metres for MOVE, radians for TURN, seconds for FOLLOW
    speed: float  # m/s for MOVE, rad/s for TURN, m/s for FOLLOW
    sign: float  # +1 forward / left, -1 backward / right
    #: FOLLOW only: who is being tracked, and how close to get before holding station.
    target_id: str = ""
    stop_distance_m: float = 0.6


@dataclass(slots=True)
class RobotProfile:
    """Physical constants of the simulated chassis. Tunable, like the real thing."""

    max_speed_mmps: int = DEFAULT_MAX_SPEED_MMPS
    max_turn_dps: int = DEFAULT_MAX_TURN_DPS
    body_radius_m: float = 0.07
    #: Where the cliff sensors sit relative to the centre: forward, and lateral offset.
    cliff_forward_m: float = 0.08
    cliff_lateral_m: float = 0.05
    #: A front obstacle closer than this counts as a bump.
    bump_range_m: float = 0.05
    #: Head travel limits. Real servos have mechanical stops; a request past one is clamped.
    head_pitch_min_deg: int = -25
    head_pitch_max_deg: int = 40
    head_yaw_limit_deg: int = 90
    #: Vertical field of view, used to turn a look_at target into a pitch angle.
    camera_vfov_deg: float = 45.0
    idle_drain_pct_per_s: float = 0.02
    move_drain_pct_per_s: float = 0.12
    charge_pct_per_s: float = 0.5
    #: Gaussian noise on distance readings, in millimetres. Zero keeps tests exact;
    #: a real sensor never reads zero-noise, so runs that care set it.
    sensor_noise_mm: float = 0.0


@dataclass
class RobotSimState:
    """Everything the simulated robot knows about itself."""

    profile: RobotProfile = field(default_factory=RobotProfile)
    x_m: float = 0.6
    y_m: float = 0.2
    yaw_rad: float = 0.0
    head_pitch_deg: int = 0
    head_yaw_deg: int = 0
    lift_pct: int = 0
    battery_pct: float = 85.0
    charging: bool = False
    volume_pct: int = 50
    emotion: str = "neutral"
    emotion_intensity_pct: int = 0
    animation: str | None = None
    animation_remaining_s: float = 0.0
    camera_active: bool = True
    listening: bool = False
    speaking: bool = False
    motion: _Motion | None = None
    distance_front_mm: int = 0
    distance_left_mm: int = 0
    distance_right_mm: int = 0
    cliff_front_left: bool = False
    cliff_front_right: bool = False
    bump: bool = False
    picked_up: bool = False
    accel_mg: tuple[int, int, int] = (0, 0, 1000)
    gyro_mdps: tuple[int, int, int] = (0, 0, 0)
    _rng: random.Random = field(default_factory=random.Random, repr=False)

    def seed(self, seed: int) -> None:
        self._rng = random.Random(seed)

    # -- commands ------------------------------------------------------------------------

    def start_move(self, distance_mm: int, speed_mmps: int) -> tuple[str, MotionResult | None]:
        """Begin a straight move. Returns the new action id and any motion it superseded."""
        speed = min(abs(speed_mmps), self.profile.max_speed_mmps) or self.profile.max_speed_mmps
        return self._start(
            MotionKind.MOVE,
            remaining=abs(distance_mm) / 1000.0,
            speed=speed / 1000.0,
            sign=-1.0 if distance_mm < 0 else 1.0,
        )

    def start_turn(self, angle_deg: int, speed_dps: int) -> tuple[str, MotionResult | None]:
        """Begin a turn in place. Positive angles are counter-clockwise (to the left)."""
        speed = min(abs(speed_dps), self.profile.max_turn_dps) or self.profile.max_turn_dps
        return self._start(
            MotionKind.TURN,
            remaining=math.radians(abs(angle_deg)),
            speed=math.radians(speed),
            sign=-1.0 if angle_deg < 0 else 1.0,
        )

    def _start(
        self, kind: MotionKind, *, remaining: float, speed: float, sign: float
    ) -> tuple[str, MotionResult | None]:
        superseded = self._end_motion(MotionOutcome.SUPERSEDED, "replaced by a new motion command")
        action_id = uuid4().hex[:12]
        if remaining <= 0:
            return action_id, superseded
        self.motion = _Motion(action_id=action_id, kind=kind, remaining=remaining, speed=speed, sign=sign)
        return action_id, superseded

    def start_follow(
        self, target_id: str, duration_ms: int, stop_distance_mm: int, speed_mmps: int | None = None
    ) -> tuple[str, MotionResult | None]:
        """Begin following a person for a bounded time.

        ``remaining`` is seconds here rather than metres: a follow terminates on its
        deadline, not on a distance, because the target moves.
        """
        speed = min(abs(speed_mmps or 200), self.profile.max_speed_mmps) or self.profile.max_speed_mmps
        superseded = self._end_motion(MotionOutcome.SUPERSEDED, "replaced by a new motion command")
        action_id = uuid4().hex[:12]
        if duration_ms <= 0:
            return action_id, superseded
        self.motion = _Motion(
            action_id=action_id,
            kind=MotionKind.FOLLOW,
            remaining=duration_ms / 1000.0,
            speed=speed / 1000.0,
            sign=1.0,
            target_id=target_id,
            stop_distance_m=stop_distance_mm / 1000.0,
        )
        return action_id, superseded

    def stop(self, detail: str = "stop requested") -> MotionResult | None:
        """Cancel the motion in flight, if any."""
        return self._end_motion(MotionOutcome.CANCELLED, detail)

    def _end_motion(self, outcome: MotionOutcome, detail: str) -> MotionResult | None:
        if self.motion is None:
            return None
        motion, self.motion = self.motion, None
        return MotionResult(action_id=motion.action_id, kind=motion.kind, outcome=outcome, detail=detail)

    # -- the step ------------------------------------------------------------------------

    def step(self, dt_s: float, world: World, *, motor_failure: bool = False) -> list[MotionResult]:
        """Advance the robot by ``dt_s`` simulated seconds. Returns motions that ended."""
        finished: list[MotionResult] = []
        self._step_power(dt_s, world)
        result = self._step_motion(dt_s, world, motor_failure=motor_failure)
        if result is not None:
            finished.append(result)
        if self.animation_remaining_s > 0:
            self.animation_remaining_s = max(0.0, self.animation_remaining_s - dt_s)
            if self.animation_remaining_s == 0:
                self.animation = None
        self.refresh_sensors(world)
        return finished

    def _step_power(self, dt_s: float, world: World) -> None:
        self.charging = world.at_dock(self.x_m, self.y_m)
        if self.charging:
            self.battery_pct = min(100.0, self.battery_pct + self.profile.charge_pct_per_s * dt_s)
            return
        drain = self.profile.move_drain_pct_per_s if self.motion is not None else self.profile.idle_drain_pct_per_s
        self.battery_pct = max(0.0, self.battery_pct - drain * dt_s)

    def _step_motion(self, dt_s: float, world: World, *, motor_failure: bool) -> MotionResult | None:
        motion = self.motion
        if motion is None:
            return None
        if motor_failure:
            return self._end_motion(MotionOutcome.MOTOR_FAILURE, "drive motors reported a fault")
        if self.battery_pct <= 0.0:
            return self._end_motion(MotionOutcome.BATTERY_EMPTY, "battery is empty")
        if motion.kind is MotionKind.FOLLOW:
            return self._step_follow(motion, dt_s, world)
        travelled = min(motion.remaining, motion.speed * dt_s)
        if motion.kind is MotionKind.TURN:
            self.yaw_rad = _wrap(self.yaw_rad + motion.sign * travelled)
        else:
            blocked = self._blocked_ahead(world, motion.sign * travelled)
            if blocked is not None:
                outcome, detail = blocked
                return self._end_motion(outcome, detail)
            self.x_m += math.cos(self.yaw_rad) * motion.sign * travelled
            self.y_m += math.sin(self.yaw_rad) * motion.sign * travelled
        motion.remaining -= travelled
        if motion.remaining <= 1e-9:
            return self._end_motion(MotionOutcome.COMPLETED, "")
        return None

    def _step_follow(self, motion: _Motion, dt_s: float, world: World) -> MotionResult | None:
        """One tick of closed-loop following: aim at the target, close the gap, hold station.

        Deliberately simple — turn towards the bearing, drive while further away than the
        stop distance, stop moving when inside it. It is a plausible firmware behaviour,
        not a controller: the point is that the backend never sees any of this.
        """
        person = world.people.get(motion.target_id)
        if person is None:
            return self._end_motion(MotionOutcome.TARGET_LOST, f"target {motion.target_id} is not in view")
        motion.remaining -= dt_s
        distance_m, bearing_rad = _bearing(self.x_m, self.y_m, self.yaw_rad, person.x_m, person.y_m)
        turn_rate = math.radians(self.profile.max_turn_dps) * dt_s
        self.yaw_rad = _wrap(self.yaw_rad + max(-turn_rate, min(turn_rate, bearing_rad)))
        if distance_m > motion.stop_distance_m and abs(bearing_rad) < math.radians(30):
            step = min(motion.speed * dt_s, distance_m - motion.stop_distance_m)
            blocked = self._blocked_ahead(world, step)
            if blocked is not None:
                outcome, detail = blocked
                return self._end_motion(outcome, detail)
            self.x_m += math.cos(self.yaw_rad) * step
            self.y_m += math.sin(self.yaw_rad) * step
        if motion.remaining <= 1e-9:
            return self._end_motion(MotionOutcome.COMPLETED, "")
        return None

    def _blocked_ahead(self, world: World, signed_step_m: float) -> tuple[MotionOutcome, str] | None:
        next_x = self.x_m + math.cos(self.yaw_rad) * signed_step_m
        next_y = self.y_m + math.sin(self.yaw_rad) * signed_step_m
        # Test the leading edge of the chassis, not its centre: a robot stops when its
        # bumper reaches the obstacle, not when its middle does.
        lead = math.copysign(self.profile.body_radius_m, signed_step_m)
        edge_x = next_x + math.cos(self.yaw_rad) * lead
        edge_y = next_y + math.sin(self.yaw_rad) * lead
        cliff_id = world.cliff_at(edge_x, edge_y)
        if cliff_id is not None:
            return MotionOutcome.CLIFF, f"cliff sensor {cliff_id} asserted"
        obstacle_id = world.obstacle_at(edge_x, edge_y)
        if obstacle_id is not None:
            return MotionOutcome.OBSTACLE, f"obstacle {obstacle_id} in the path"
        if world.crosses_wall(self.x_m, self.y_m, edge_x, edge_y):
            return MotionOutcome.OBSTACLE, "wall in the path"
        return None

    def refresh_sensors(self, world: World) -> None:
        """Re-read every sensor from the world. Called at the end of each step."""
        self.distance_front_mm = self._range(world, 0.0)
        self.distance_left_mm = self._range(world, math.radians(60))
        self.distance_right_mm = self._range(world, math.radians(-60))
        forward, lateral = self.profile.cliff_forward_m, self.profile.cliff_lateral_m
        self.cliff_front_left = world.cliff_at(*self._offset(forward, lateral)) is not None
        self.cliff_front_right = world.cliff_at(*self._offset(forward, -lateral)) is not None
        self.bump = self.distance_front_mm <= int(self.profile.bump_range_m * 1000)
        speed = self.linear_speed_mps
        self.accel_mg = (int(speed * 1000), 0, 1000)
        self.gyro_mdps = (0, 0, int(math.degrees(self.angular_speed_rps) * 1000))

    def _offset(self, forward_m: float, lateral_m: float) -> tuple[float, float]:
        cos_yaw, sin_yaw = math.cos(self.yaw_rad), math.sin(self.yaw_rad)
        return (
            self.x_m + cos_yaw * forward_m - sin_yaw * lateral_m,
            self.y_m + sin_yaw * forward_m + cos_yaw * lateral_m,
        )

    def _range(self, world: World, relative_rad: float) -> int:
        metres = world.distance(self.x_m, self.y_m, self.yaw_rad + relative_rad)
        noise = self.profile.sensor_noise_mm
        millimetres = metres * 1000 + (self._rng.gauss(0.0, noise) if noise else 0.0)
        return max(0, int(round(millimetres)))

    # -- views ---------------------------------------------------------------------------

    @property
    def moving(self) -> bool:
        return self.motion is not None

    @property
    def action_id(self) -> str | None:
        return self.motion.action_id if self.motion is not None else None

    @property
    def linear_speed_mps(self) -> float:
        motion = self.motion
        if motion is None or motion.kind not in (MotionKind.MOVE, MotionKind.FOLLOW):
            return 0.0
        return motion.speed * motion.sign

    @property
    def angular_speed_rps(self) -> float:
        motion = self.motion
        if motion is None or motion.kind is not MotionKind.TURN:
            return 0.0
        return motion.speed * motion.sign

    @property
    def cliff_detected(self) -> bool:
        return self.cliff_front_left or self.cliff_front_right

    @property
    def activity(self) -> str:
        if self.charging:
            return "charging"
        if self.moving:
            return "moving"
        if self.speaking:
            return "speaking"
        if self.listening:
            return "listening"
        return "idle"

    def pose_payload(self) -> dict[str, Any]:
        return {
            "x_mm": int(round(self.x_m * 1000)),
            "y_mm": int(round(self.y_m * 1000)),
            "yaw_deg": int(round(math.degrees(self.yaw_rad))),
            "frame": "odom",
        }

    def motion_payload(self) -> dict[str, Any]:
        return {
            "moving": self.moving,
            "action_id": self.action_id,
            "linear_speed_mmps": int(round(self.linear_speed_mps * 1000)),
            "angular_speed_mdps": int(round(math.degrees(self.angular_speed_rps) * 1000)),
        }

    def battery_payload(self) -> dict[str, Any]:
        return {
            "percent": int(round(self.battery_pct)),
            "charging": self.charging,
            "battery_mv": int(round(3300 + self.battery_pct * 9)),
        }

    def sensor_payload(self) -> dict[str, Any]:
        return {
            "cliff_detected": self.cliff_detected,
            "bump_detected": self.bump,
            "picked_up": self.picked_up,
            "touch_detected": False,
            "readings": {
                "front_mm": self.distance_front_mm,
                "left_mm": self.distance_left_mm,
                "right_mm": self.distance_right_mm,
                "head_pitch_deg": self.head_pitch_deg,
                "head_yaw_deg": self.head_yaw_deg,
                "lift_pct": self.lift_pct,
            },
        }

    def imu_payload(self) -> dict[str, Any]:
        return {
            "accel_mg": {"x": self.accel_mg[0], "y": self.accel_mg[1], "z": self.accel_mg[2]},
            "gyro_mdps": {"x": self.gyro_mdps[0], "y": self.gyro_mdps[1], "z": self.gyro_mdps[2]},
            "pitch_deg": 0,
            "roll_deg": 0,
            "yaw_deg": int(round(math.degrees(self.yaw_rad))),
        }

    def cliff_payload(self) -> dict[str, Any]:
        return {
            "detected": self.cliff_detected,
            "front_left": self.cliff_front_left,
            "front_right": self.cliff_front_right,
        }

    def expression_payload(self) -> dict[str, Any]:
        return {
            "emotion": self.emotion,
            "intensity_pct": self.emotion_intensity_pct,
            "animation": self.animation,
        }

    def audio_payload(self) -> dict[str, Any]:
        return {"listening": self.listening, "speaking": self.speaking, "volume_pct": self.volume_pct}

    def vision_payload(self, world: World) -> dict[str, Any]:
        people = [entry for entry in world.visible(self.x_m, self.y_m, self.yaw_rad) if entry["kind"] == "person"]
        return {"camera_active": self.camera_active, "faces_detected": len(people)}

    def telemetry_payload(self, world: World) -> dict[str, Any]:
        """Everything, in one notification. The periodic frame."""
        return {
            "pose": self.pose_payload(),
            "motion": self.motion_payload(),
            "battery": self.battery_payload(),
            "sensors": self.sensor_payload(),
            "audio": self.audio_payload(),
            "vision": self.vision_payload(world),
            "expression": self.expression_payload(),
            "activity": {"activity": self.activity, "detail": self.animation},
        }

    def snapshot(self, world: World) -> dict[str, Any]:
        """The full ``robot.get_status`` answer, and the body of the status endpoint."""
        return {
            "pose": self.pose_payload(),
            "motion": self.motion_payload(),
            "battery": self.battery_payload(),
            "sensors": self.sensor_payload(),
            "cliff": self.cliff_payload(),
            "imu": self.imu_payload(),
            "audio": self.audio_payload(),
            "expression": self.expression_payload(),
            "head": {"pitch_deg": self.head_pitch_deg, "yaw_deg": self.head_yaw_deg},
            "lift": {"height_pct": self.lift_pct},
            "activity": self.activity,
            "visible": world.visible(self.x_m, self.y_m, self.yaw_rad),
            "world": world.summary(),
        }
