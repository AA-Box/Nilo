"""Turning a tracked target into robot commands: look-at, and following.

Vision produces normalized image coordinates. This is where they become a head angle or a
drive command — and it lives in ``robot/behavior/`` rather than in ``robot/vision/``
because it commands the robot, and perception is not allowed to
(docs/robot-architecture.md Sect. 7).

Both controllers are expressed as **pure decision functions**. ``decide_look_at`` and
``decide_follow`` take the target state, the clock and the tuning, and return what should
be commanded; a thin caller applies it. No awaits, no clock reads, no hidden state — which
is what makes "the head does not jitter" and "it stops at 600 mm" exact tests rather than
demos.

Three rules keep a camera-driven controller from shaking the robot apart:

* **A deadband.** An error smaller than the deadband is not an error, it is noise. Below
  it, nothing is commanded at all.
* **A rate limit.** At most one command per interval, whatever perception reports in
  between. A 30 Hz tracker must not become a 30 Hz servo command stream.
* **Proportional steps with a ceiling.** Command a fraction of the error, never all of it,
  and never more than the maximum step. Overshoot is what makes tracking oscillate.

Nothing here decides *whether* to follow somebody — that is a behaviour, and a scored one.
This decides how, once something has.
"""

from __future__ import annotations

from dataclasses import dataclass

from robot.behavior.tuning import BehaviorTuning
from robot.state.world import ImagePoint


@dataclass(frozen=True)
class LookAtDecision:
    """What, if anything, to command the head. ``send`` false means "stay where you are"."""

    send: bool
    x_pct: int = 50
    y_pct: int = 50
    reason: str = ""

    def __bool__(self) -> bool:
        return self.send


@dataclass(frozen=True)
class FollowDecision:
    """One step of following. All three fields may be zero: holding station is a decision."""

    turn_deg: int = 0
    move_mm: int = 0
    stop: bool = False
    reason: str = ""

    @property
    def acts(self) -> bool:
        return self.stop or bool(self.turn_deg) or bool(self.move_mm)


def decide_look_at(
    point: ImagePoint,
    *,
    now: float,
    last_command_at: float | None,
    tuning: BehaviorTuning,
) -> LookAtDecision:
    """Where to point the head, given where the target is in the frame.

    The command is a *point in the frame*, in percent — the same vocabulary the device
    tool takes — moved a fraction of the way towards the target rather than all of it, so
    a noisy detection cannot slam the head across its travel.
    """
    if last_command_at is not None and now - last_command_at < tuning.look_at_min_interval_s:
        return LookAtDecision(False, reason="rate limited")
    dx, dy = point.offset_from_centre()
    if max(abs(dx), abs(dy)) <= tuning.look_at_deadband:
        return LookAtDecision(False, reason="already centred")
    gain = tuning.look_at_gain
    target_x = 0.5 + dx * gain
    target_y = 0.5 + dy * gain
    return LookAtDecision(
        True,
        x_pct=_percent(target_x),
        y_pct=_percent(target_y),
        reason=f"target {dx:+.2f},{dy:+.2f} off centre",
    )


def decide_follow(
    *,
    offset_x: float,
    distance_mm: int,
    now: float,
    last_command_at: float | None,
    tuning: BehaviorTuning,
    blocked: bool = False,
) -> FollowDecision:
    """One leg of following a target. Deterministic, and bounded in every direction.

    ``offset_x`` is the target's horizontal offset from the centre of the frame, -0.5..0.5.
    ``distance_mm`` is the best estimate available; with one camera that is a rough number
    (``robot/vision/types.py``), which is exactly why the stop band has hysteresis instead
    of a single threshold.
    """
    if blocked:
        return FollowDecision(stop=True, reason="a sensor says the way is blocked")
    if last_command_at is not None and now - last_command_at < tuning.follow_min_interval_s:
        return FollowDecision(reason="rate limited")

    turn = 0
    if abs(offset_x) > tuning.follow_turn_deadband:
        # Half the camera's field of view maps the full 0.5 offset, so a target at the
        # edge of the frame is a half-FOV turn — before the gain and the ceiling. The sign
        # flips because image space and the robot's frame disagree: a target left of
        # centre is a *negative* x offset and a *positive* (left) turn, the same
        # convention `robot/vision/types.py:position_from_box` uses for bearing.
        desired = -offset_x * tuning.camera_fov_deg
        turn = _bounded(desired * tuning.follow_turn_gain, tuning.follow_max_turn_deg)

    stop_distance = tuning.follow_stop_distance_mm
    band = tuning.follow_stop_band_mm
    move = 0
    if distance_mm > stop_distance + band:
        move = min(tuning.follow_step_mm, distance_mm - stop_distance)
    elif distance_mm < stop_distance - band:
        move = -min(tuning.follow_step_mm, stop_distance - distance_mm)

    if not turn and not move:
        return FollowDecision(reason="holding station")
    return FollowDecision(
        turn_deg=turn,
        move_mm=int(move),
        reason=f"target {offset_x:+.2f} off centre at ~{distance_mm}mm",
    )


def _percent(value: float) -> int:
    return int(round(min(1.0, max(0.0, value)) * 100))


def _bounded(value: float, ceiling: int) -> int:
    return int(round(max(-ceiling, min(ceiling, value))))


__all__ = ["FollowDecision", "LookAtDecision", "decide_follow", "decide_look_at"]
