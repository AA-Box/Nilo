"""Robot safety: the backend half of a two-layer design, and the weaker half by construction.

This package clamps nothing and guarantees nothing. It **admits or rejects** action
requests against configured limits and the current world picture, latches an emergency
stop, and supervises deadlines so an action that never reports back is marked
``TIMED_OUT`` and a stop is attempted.

    from robot.safety import RobotSafetyPolicy, SafetyContext, SafetyLimits

    policy = RobotSafetyPolicy(SafetyLimits(max_distance_mm=800))
    decision = policy.evaluate(spec, ActionSource.LLM, SafetyContext(robot_id, state, utcnow()))
    if not decision:
        ...  # decision.reason is a typed RejectionReason

**Firmware owns every guarantee.** Cliff protection, the motor watchdog, current and
thermal protection, physical motion bounds, acceleration constraints and the local
emergency stop are implemented again, independently, on the robot — because this process
can be killed mid-motion, its event loop can stall for an unbounded time, and a command
the device has already accepted cannot be recalled from here. docs/safety-model.md states which
protection lives where and what each one does when the Python process dies.

Layering (docs/robot-architecture.md Sect. 7): this package imports ``robot/state`` and
nothing else from the subsystem. It must never import ``robot/actions``,
``robot/behavior`` or ``robot/personality`` — safety sits below them so that personality
may influence *which* action is chosen and never *whether* it is allowed. The rule is
enforced by ``robot/.ruff.toml`` and asserted by ``tests/robot/test_layering.py``.
"""

from robot.safety.estop import EmergencyStop, EmergencyStopState
from robot.safety.limits import DEFAULT_LIMITS_PATH, SafetyLimits, limits_from_mapping, load_limits
from robot.safety.policy import Request, RobotSafetyPolicy, SafetyContext, SafetyDecision
from robot.safety.watchdog import DEFAULT_INTERVAL_S, Watchdog

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_LIMITS_PATH",
    "EmergencyStop",
    "EmergencyStopState",
    "Request",
    "RobotSafetyPolicy",
    "SafetyContext",
    "SafetyDecision",
    "SafetyLimits",
    "Watchdog",
    "limits_from_mapping",
    "load_limits",
]
