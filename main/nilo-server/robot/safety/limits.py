"""Configurable safety limits, and where they are allowed to come from.

Every number the policy compares against lives here, with a default that is conservative
rather than capable. Two rules about *where* these values may be stored, both of them
properties of the inherited server rather than preferences:

* **Not the server config dict.** In manager-api mode the local configuration is replaced
  wholesale by the API response and only the ``server`` and ``manager-api`` blocks survive
  (``config/config_loader.py:get_config_from_api_async``). A speed ceiling stored there
  would silently vanish in exactly the deployment that has the most robots in it.
* **Not a module-level singleton.** Limits are constructed per runtime and passed in, so
  two robots with different chassis can hold different ceilings in one process and a test
  never inherits another test's numbers.

The file format is a small YAML document; :func:`load_limits` reads one and falls back to
the defaults when the path does not exist, because a missing limits file must leave a
conservative policy in place, not no policy at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: Where ``load_limits()`` looks when it is given no path. Relative to the server
#: directory, like the rest of the runtime's data files.
DEFAULT_LIMITS_PATH = Path("data") / "robot_limits.yaml"


class SafetyLimits(BaseModel):
    """The bounds a request is judged against. Conservative defaults, all overridable.

    Units are in the field names, as everywhere else in the robot vocabulary. A limit of
    zero means "reject everything of this kind"; there is no sentinel for "unlimited",
    because an unlimited motion bound is not a limit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- motion bounds ---------------------------------------------------------------------
    #: Longest single drive command. Not a total distance budget: one command.
    max_distance_mm: int = Field(default=1000, ge=0)
    #: Largest single turn. A robot that needs 540° issues two commands and can be stopped
    #: between them.
    max_angle_deg: int = Field(default=180, ge=0)
    max_speed_mmps: int = Field(default=300, ge=0)
    max_turn_speed_dps: int = Field(default=120, ge=0)
    #: Longest a single follow may run before it has to be re-requested.
    max_follow_duration_ms: int = Field(default=30_000, ge=0)
    max_animation_duration_ms: int = Field(default=30_000, ge=0)

    # -- sensor gating ---------------------------------------------------------------------
    #: A forward move is refused when the front distance reading is below this.
    min_obstacle_distance_mm: int = Field(default=250, ge=0)
    #: How old sensor data may be and still be acted on. Past this, a motion request is
    #: rejected as :attr:`~robot.state.actions.RejectionReason.SENSOR_DATA_STALE` — the
    #: world model is a cache, and an old cliff reading is not a cliff reading.
    max_sensor_age_s: float = Field(default=2.0, gt=0)
    #: How long the session may be silent before motion is refused and running motion is
    #: stopped. The device's own watchdog is the guarantee; this is the server noticing.
    heartbeat_timeout_s: float = Field(default=5.0, gt=0)
    #: Below this battery percentage, motion is refused. Charging robots are exempt.
    min_battery_percent: int = Field(default=5, ge=0, le=100)

    # -- time bounds -----------------------------------------------------------------------
    #: How long a request may sit between submission and dispatch before it is stale.
    #: A "come here" that waited twenty seconds behind a queue is not the same request.
    action_ttl_s: float = Field(default=15.0, gt=0)
    #: Completion budget applied when a caller supplies none and the spec cannot estimate.
    default_timeout_s: float = Field(default=10.0, gt=0)
    #: Ceiling on any caller-supplied timeout, so a long timeout cannot disable the watchdog.
    max_timeout_s: float = Field(default=60.0, gt=0)

    # -- rate limits -----------------------------------------------------------------------
    #: Most motion commands admitted in :attr:`rate_window_s`. A looping behaviour or a
    #: confused model cannot emit commands faster than the device can retire them.
    max_motion_per_window: int = Field(default=10, ge=0)
    rate_window_s: float = Field(default=10.0, gt=0)

    def with_overrides(self, **overrides: Any) -> SafetyLimits:
        """A copy with some fields replaced. Validated, so a bad override raises here."""
        return SafetyLimits(**{**self.model_dump(), **overrides})


def limits_from_mapping(data: Any) -> SafetyLimits:
    """Build limits from a parsed document. Unknown keys raise rather than being ignored.

    Strict on purpose: a typo in a limits file is the kind of mistake that silently leaves
    the default ceiling in place, which is the failure this whole layer exists to avoid.
    """
    if data is None:
        return SafetyLimits()
    if not isinstance(data, dict):
        raise ValueError("the limits document must be a mapping")
    block = data.get("limits", data)
    if not isinstance(block, dict):
        raise ValueError("the 'limits' key must hold a mapping")
    return SafetyLimits(**{str(key): value for key, value in block.items()})


def load_limits(path: str | Path | None = None) -> SafetyLimits:
    """Read a limits file, or return the defaults when there is none.

    A file that exists but cannot be parsed raises: silently falling back to defaults
    after an operator wrote a limits file is worse than failing to start.
    """
    location = Path(path) if path is not None else DEFAULT_LIMITS_PATH
    if not location.exists():
        logger.info("no robot limits file at %s; using the built-in conservative defaults", location)
        return SafetyLimits()
    import yaml

    with location.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    limits = limits_from_mapping(document)
    logger.info("robot safety limits loaded from %s", location)
    return limits


__all__ = ["DEFAULT_LIMITS_PATH", "SafetyLimits", "limits_from_mapping", "load_limits"]
