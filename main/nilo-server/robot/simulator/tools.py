"""The tool table the simulated robot publishes over device MCP.

This is the firmware contract as the backend will see it, so it follows the vocabulary
rules the action layer is held to (docs/robot-roadmap.md Phase 4): every parameter is an
integer, a bounded string or a boolean, never a float; the unit is in the name
(``distance_mm``, ``angle_deg``, ``speed_mmps``); allowed string values are enumerated in
the description; everything optional has a default. Nothing here names a raw actuator —
no PWM, no servo microseconds, no wheel speeds — because a tool that did would hand the
LLM the motors.

Tool names are dotted (``robot.motion.move``). The server sanitizes them for the LLM
function namespace (``robot_motion_move``) and calls back with the raw name, which is
exactly the round trip a real device exercises.
"""

from __future__ import annotations

import base64
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from robot.simulator.camera import CameraFailure
from robot.simulator.scenarios import Faults
from robot.simulator.state import RobotSimState
from robot.simulator.world import World

logger = logging.getLogger(__name__)

#: Emotions the expression tool accepts. Anything else is stored but logged as unknown.
EMOTIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "sad",
    "angry",
    "surprised",
    "curious",
    "bored",
    "tired",
    "scared",
    "content",
)


class ToolHost(Protocol):
    """What a tool handler may touch on the running simulator."""

    world: World
    state: RobotSimState
    faults: Faults

    def command_move(self, distance_mm: int, speed_mmps: int) -> str: ...

    def command_turn(self, angle_deg: int, speed_dps: int) -> str: ...

    def command_stop(self) -> None: ...

    async def capture_frame(self, question: str | None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ToolReply:
    """An MCP ``tools/call`` result, before it goes on the wire."""

    payload: dict[str, Any]
    is_error: bool = False
    extra_content: tuple[dict[str, Any], ...] = ()

    def to_result(self) -> dict[str, Any]:
        if self.is_error:
            return {"isError": True, "error": str(self.payload.get("error", "tool failed"))}
        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(self.payload, separators=(",", ":"))}]
        content.extend(self.extra_content)
        return {"content": content}


Handler = Callable[[ToolHost, dict[str, Any]], Awaitable[ToolReply]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One published tool: its MCP definition and the handler behind it."""

    name: str
    description: str
    handler: Handler
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": dict(self.properties),
                "required": list(self.required),
            },
        }


def _int(args: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    """One integer argument, clamped. Firmware clamps; it does not reject and stop."""
    raw = args.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None
    return max(low, min(high, value))


# -- handlers -----------------------------------------------------------------------------


async def _get_status(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    return ToolReply(host.state.snapshot(host.world))


async def _move(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    distance_mm = _int(args, "distance_mm", 0, low=-2000, high=2000)
    speed_mmps = _int(args, "speed_mmps", 200, low=20, high=host.state.profile.max_speed_mmps)
    if distance_mm == 0:
        return ToolReply({"error": "distance_mm must not be zero"}, is_error=True)
    action_id = host.command_move(distance_mm, speed_mmps)
    return ToolReply(
        {
            "accepted": True,
            "action_id": action_id,
            "state": "moving",
            "distance_mm": distance_mm,
            "speed_mmps": speed_mmps,
            "eta_ms": int(abs(distance_mm) / speed_mmps * 1000),
        }
    )


async def _turn(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    angle_deg = _int(args, "angle_deg", 0, low=-360, high=360)
    speed_dps = _int(args, "speed_dps", 90, low=10, high=host.state.profile.max_turn_dps)
    if angle_deg == 0:
        return ToolReply({"error": "angle_deg must not be zero"}, is_error=True)
    action_id = host.command_turn(angle_deg, speed_dps)
    return ToolReply(
        {
            "accepted": True,
            "action_id": action_id,
            "state": "moving",
            "angle_deg": angle_deg,
            "speed_dps": speed_dps,
            "eta_ms": int(abs(angle_deg) / speed_dps * 1000),
        }
    )


async def _stop(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    was = host.state.action_id
    host.command_stop()
    return ToolReply({"accepted": True, "stopped_action_id": was, "state": "idle"})


async def _head_set_angle(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    profile = host.state.profile
    pitch = _int(
        args, "pitch_deg", host.state.head_pitch_deg,
        low=profile.head_pitch_min_deg, high=profile.head_pitch_max_deg,
    )
    yaw = _int(
        args, "yaw_deg", host.state.head_yaw_deg,
        low=-profile.head_yaw_limit_deg, high=profile.head_yaw_limit_deg,
    )
    host.state.head_pitch_deg = pitch
    host.state.head_yaw_deg = yaw
    return ToolReply({"accepted": True, "pitch_deg": pitch, "yaw_deg": yaw})


async def _head_look_at(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    """Aim the head at a point in the camera frame, given as percentages of width/height.

    Normalized coordinates rather than pixels, so a caller does not have to know the
    camera resolution (docs/robot-roadmap.md Phase 5), and integers rather than 0.0-1.0
    floats, because the firmware vocabulary has no float type.
    """
    profile = host.state.profile
    x_pct = _int(args, "x_pct", 50, low=0, high=100)
    y_pct = _int(args, "y_pct", 50, low=0, high=100)
    half_h = host.world.spec.camera_fov_deg / 2
    half_v = profile.camera_vfov_deg / 2
    yaw = host.state.head_yaw_deg + int(round((50 - x_pct) / 50 * half_h))
    pitch = host.state.head_pitch_deg + int(round((50 - y_pct) / 50 * half_v))
    host.state.head_yaw_deg = max(-profile.head_yaw_limit_deg, min(profile.head_yaw_limit_deg, yaw))
    host.state.head_pitch_deg = max(profile.head_pitch_min_deg, min(profile.head_pitch_max_deg, pitch))
    return ToolReply(
        {
            "accepted": True,
            "x_pct": x_pct,
            "y_pct": y_pct,
            "pitch_deg": host.state.head_pitch_deg,
            "yaw_deg": host.state.head_yaw_deg,
        }
    )


async def _lift_set_position(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    height_pct = _int(args, "height_pct", 0, low=0, high=100)
    host.state.lift_pct = height_pct
    return ToolReply({"accepted": True, "height_pct": height_pct})


async def _expression_set(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    emotion = str(args.get("emotion", "neutral"))
    if emotion not in EMOTIONS:
        logger.info("expression %r is not in the published vocabulary; setting it anyway", emotion)
    host.state.emotion = emotion
    host.state.emotion_intensity_pct = _int(args, "intensity_pct", 100, low=0, high=100)
    return ToolReply(
        {"accepted": True, "emotion": emotion, "intensity_pct": host.state.emotion_intensity_pct}
    )


async def _animation_play(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    name = str(args.get("name", "")).strip()
    if not name:
        return ToolReply({"error": "name is required"}, is_error=True)
    duration_ms = _int(args, "duration_ms", 2000, low=100, high=30000)
    host.state.animation = name
    host.state.animation_remaining_s = duration_ms / 1000.0
    return ToolReply({"accepted": True, "name": name, "duration_ms": duration_ms})


async def _camera_capture(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    question = args.get("question")
    try:
        frame = await host.capture_frame(str(question) if question else None)
    except CameraFailure as exc:
        return ToolReply({"error": f"camera failure: {exc}"}, is_error=True)
    image_b64: str = frame["image_base64"]
    payload = {key: value for key, value in frame.items() if key != "image_base64"}
    payload["image_base64"] = image_b64
    payload["image_bytes"] = len(base64.b64decode(image_b64))
    return ToolReply(
        payload,
        extra_content=({"type": "image", "data": image_b64, "mimeType": frame["mime_type"]},),
    )


async def _audio_set_volume(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    volume_pct = _int(args, "volume_pct", 50, low=0, high=100)
    host.state.volume_pct = volume_pct
    return ToolReply({"accepted": True, "volume_pct": volume_pct})


async def _sensor_get_distance(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    host.state.refresh_sensors(host.world)
    return ToolReply(
        {
            "front_mm": host.state.distance_front_mm,
            "left_mm": host.state.distance_left_mm,
            "right_mm": host.state.distance_right_mm,
            "max_range_mm": int(host.world.max_range_m * 1000),
            "bump_detected": host.state.bump,
        }
    )


async def _sensor_get_imu(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    return ToolReply(host.state.imu_payload())


async def _sensor_get_cliff(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    host.state.refresh_sensors(host.world)
    return ToolReply(host.state.cliff_payload())


async def _power_get_battery(host: ToolHost, args: dict[str, Any]) -> ToolReply:
    payload = dict(host.state.battery_payload())
    dock = host.world.dock_bearing(host.state.x_m, host.state.y_m, host.state.yaw_rad)
    if dock is not None:
        distance_m, bearing_rad = dock
        payload["dock_distance_mm"] = int(round(distance_m * 1000))
        payload["dock_bearing_deg"] = int(round(math.degrees(bearing_rad)))
    return ToolReply(payload)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="robot.get_status",
        description=(
            "Everything the robot knows about itself right now: pose, motion, battery, sensors, head, "
            "lift, expression and what it can see."
        ),
        handler=_get_status,
    ),
    ToolSpec(
        name="robot.motion.move",
        description=(
            "Drive straight. Returns immediately with an action_id; completion arrives as "
            "notifications/motion_completed or notifications/motion_failed."
        ),
        handler=_move,
        properties={
            "distance_mm": {
                "type": "integer",
                "description": "Distance to travel in millimetres, -2000 to 2000. Negative drives backwards.",
            },
            "speed_mmps": {
                "type": "integer",
                "description": "Speed in millimetres per second, 20 to 400. Default 200.",
            },
        },
        required=("distance_mm",),
    ),
    ToolSpec(
        name="robot.motion.turn",
        description="Turn in place. Returns immediately with an action_id; completion arrives as a notification.",
        handler=_turn,
        properties={
            "angle_deg": {
                "type": "integer",
                "description": "Angle in degrees, -360 to 360. Positive turns left (counter-clockwise).",
            },
            "speed_dps": {
                "type": "integer",
                "description": "Turn rate in degrees per second, 10 to 180. Default 90.",
            },
        },
        required=("angle_deg",),
    ),
    ToolSpec(
        name="robot.motion.stop",
        description=(
            "Cancel the motion in flight. The cancelled action reports notifications/motion_failed "
            "with reason cancelled."
        ),
        handler=_stop,
    ),
    ToolSpec(
        name="robot.head.set_angle",
        description=(
            "Point the head at an absolute angle. Values outside the mechanical range are clamped, "
            "not rejected."
        ),
        handler=_head_set_angle,
        properties={
            "pitch_deg": {"type": "integer", "description": "Head pitch in degrees, -25 (down) to 40 (up)."},
            "yaw_deg": {"type": "integer", "description": "Head yaw in degrees, -90 (right) to 90 (left)."},
        },
    ),
    ToolSpec(
        name="robot.head.look_at",
        description=(
            "Point the head at a spot in the camera frame, given as percentages so it is independent "
            "of resolution."
        ),
        handler=_head_look_at,
        properties={
            "x_pct": {
                "type": "integer",
                "description": "Horizontal position in the frame, 0 (left edge) to 100 (right edge).",
            },
            "y_pct": {
                "type": "integer",
                "description": "Vertical position in the frame, 0 (top) to 100 (bottom).",
            },
        },
        required=("x_pct", "y_pct"),
    ),
    ToolSpec(
        name="robot.lift.set_position",
        description="Raise or lower the lift.",
        handler=_lift_set_position,
        properties={
            "height_pct": {"type": "integer", "description": "Lift height, 0 (fully down) to 100 (fully up)."}
        },
        required=("height_pct",),
    ),
    ToolSpec(
        name="robot.expression.set",
        description="Show an emotion on the face.",
        handler=_expression_set,
        properties={
            "emotion": {
                "type": "string",
                "description": "One of: " + ", ".join(EMOTIONS) + ".",
            },
            "intensity_pct": {"type": "integer", "description": "Strength, 0 to 100. Default 100."},
        },
        required=("emotion",),
    ),
    ToolSpec(
        name="robot.animation.play",
        description=(
            "Play a named animation. Unknown names are accepted and reported back, the way firmware "
            "with a data-driven animation table behaves."
        ),
        handler=_animation_play,
        properties={
            "name": {
                "type": "string",
                "description": "Animation name, for example greet, look_around, idle_fidget, sigh.",
            },
            "duration_ms": {"type": "integer", "description": "How long to play it, 100 to 30000. Default 2000."},
        },
        required=("name",),
    ),
    ToolSpec(
        name="robot.camera.capture",
        description=(
            "Capture one camera frame as a PNG. Returns base64 image data the server can decode; "
            "with a question set, the frame is also sent to the server's vision endpoint and its "
            "answer is returned."
        ),
        handler=_camera_capture,
        properties={
            "question": {
                "type": "string",
                "description": (
                    "Optional question about the frame. When set, the frame is uploaded to the vision "
                    "endpoint the server handed over during the MCP handshake."
                ),
            }
        },
    ),
    ToolSpec(
        name="robot.audio.set_volume",
        description="Set the speaker volume.",
        handler=_audio_set_volume,
        properties={"volume_pct": {"type": "integer", "description": "Volume, 0 to 100."}},
        required=("volume_pct",),
    ),
    ToolSpec(
        name="robot.sensor.get_distance",
        description="Read the forward-facing distance sensors.",
        handler=_sensor_get_distance,
    ),
    ToolSpec(
        name="robot.sensor.get_imu",
        description="Read the inertial sensor: acceleration, rotation rate and orientation.",
        handler=_sensor_get_imu,
    ),
    ToolSpec(
        name="robot.sensor.get_cliff",
        description="Read the downward-facing cliff sensors.",
        handler=_sensor_get_cliff,
    ),
    ToolSpec(
        name="robot.power.get_battery",
        description="Read the battery state and, when a dock is known, where it is.",
        handler=_power_get_battery,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}
TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)
