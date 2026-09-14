"""One file, one model, one hierarchy: everything the robot subsystem is configured by.

    from robot.config import RobotConfig

    settings = RobotConfig.load()            # data/robot.yaml, or the defaults
    settings.safety.max_speed_mmps           # 300

Before this there were five loaders — ``load_limits``, ``load_tuning``, ``load_traits``,
the memory database path and the animation directory — each with its own default path
under ``data/`` and its own answer to "what happens when the file is missing". They are
still there and still work; what is here is the one place that reads them all, so a
deployment has *one* file to write and one page to read (docs/configuration.md).

**The hierarchy**, lowest precedence first:

1. the built-in defaults, which are conservative rather than capable
2. ``data/robot.yaml`` (or ``$NILO_ROBOT_CONFIG``), one document with one section per area
3. the legacy per-area files, for a section the main document does not mention
4. ``NILO_ROBOT_*`` environment variables
5. a secret file named by a ``*_file`` key or a ``*_FILE`` variable

**Secrets are never in the file.** A value may be given as a path to read it from instead —
``admin_token_file: /run/secrets/nilo-admin-token`` — which is what a container orchestrator
mounts and what keeps a token out of a repository. :func:`resolve_secret` is the one place
that reads one.

**Not the server config dict.** The robot's numbers deliberately do not live in
``config.yaml``: in manager-api mode the local configuration is replaced wholesale by the
API response and only the ``server`` and ``manager-api`` blocks survive
(``config/config_loader.py``). A speed ceiling stored there would silently vanish in
exactly the deployment that has the most robots in it. Audio, the language model and the
provider stack stay in ``config.yaml``, because those are the inherited server's and it
owns their lifecycle.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.behavior.tuning import DEFAULT_TUNING_PATH, BehaviorTuning
from robot.memory.store import DEFAULT_DB_PATH
from robot.personality.traits import DEFAULT_PERSONALITY_PATH, PersonalityTraits
from robot.safety.limits import DEFAULT_LIMITS_PATH, SafetyLimits

logger = logging.getLogger(__name__)

#: The one document. Relative to the server directory, like every other runtime data file.
DEFAULT_CONFIG_PATH = Path("data") / "robot.yaml"

#: The variable that points somewhere else. Named like the server's own ``NILO_CONFIG``.
CONFIG_PATH_ENV = "NILO_ROBOT_CONFIG"

#: Prefix for the scalar overrides. ``NILO_ROBOT_SAFETY_MAX_SPEED_MMPS=200`` sets
#: ``safety.max_speed_mmps``; the section is the first word after the prefix. The
#: ``robot`` section may be addressed without repeating itself, so the runtime settings
#: are ``NILO_ROBOT_AUTONOMY`` rather than ``NILO_ROBOT_ROBOT_AUTONOMY``.
ENV_PREFIX = "NILO_ROBOT_"

#: ``NILO_ROBOT_*`` names that belong to something other than this model. The management
#: API reads its own (``robot/api/security.py``) and must not be warned about here.
FOREIGN_ENV_NAMES: frozenset[str] = frozenset(
    {
        "NILO_ROBOT_CONFIG",
        "NILO_ROBOT_ADMIN_TOKEN",
        "NILO_ROBOT_ADMIN_TOKEN_FILE",
        "NILO_ROBOT_API_HOST",
        "NILO_ROBOT_API_PORT",
        "NILO_ROBOT_API_ALLOW_REMOTE_CONTROL",
    }
)

class MemorySettings(BaseModel):
    """Long-term memory. Off by default: a runtime must not create a database by existing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Whether to open a store at all. With this false the robot has working memory only,
    #: ``robot_remember`` fails with a sentence a model can repeat, and nothing is written
    #: to disk. That is a deployment decision, not a degradation.
    enabled: bool = False
    #: Where the SQLite database lives. ``:memory:`` for a store that dies with the process.
    path: str = str(DEFAULT_DB_PATH)
    #: Consolidate episodes into semantic facts on this interval. 0 disables it.
    consolidation_interval_s: float = Field(default=0.0, ge=0.0)


class VisionSettings(BaseModel):
    """Perception. Also off by default, and for the same reason: it costs a camera call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Whether to run the perception loop for a connected robot at all.
    enabled: bool = False
    #: Seconds between frames. The pipeline is snapshot-based, not a video stream.
    interval_s: float = Field(default=1.0, gt=0.0)
    #: ``null``, ``colour-blob`` or ``yolo``. The default finds nothing, successfully: a
    #: deployment with no model gets a pipeline that runs and leaves every behaviour that
    #: needs a person quiet.
    detector: str = "null"
    #: For ``yolo``: the weights file. Ignored by every other detector.
    model_path: str = ""


class PersonalitySettings(BaseModel):
    """The traits, and whether they survive a restart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    traits: PersonalityTraits = Field(default_factory=PersonalityTraits)
    #: Where per-robot snapshots are written. Empty keeps personality in memory only.
    store_dir: str = ""


class RuntimeSettings(BaseModel):
    """The control plane itself: what it waits for, and how much it is allowed to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How long capability discovery may take before it is abandoned, in seconds.
    discovery_timeout_s: float = Field(default=10.0, gt=0.0)
    #: ``off``, ``passive``, ``normal`` or ``full``. What autonomy new robots start in.
    autonomy: str = "normal"
    #: Whether the behaviour engine starts ticking when a robot connects. Off by default:
    #: a robot that starts moving because it was plugged in is a robot nobody asked.
    autostart_behaviors: bool = False
    #: Seconds between behaviour ticks once it is started.
    behavior_interval_s: float = Field(default=0.5, gt=0.0)
    #: Extra animation directory, on top of the built-in library.
    animation_dir: str = ""


class RobotConfig(BaseModel):
    """Everything, in one frozen object built once at startup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    robot: RuntimeSettings = Field(default_factory=RuntimeSettings)
    safety: SafetyLimits = Field(default_factory=SafetyLimits)
    behavior: BehaviorTuning = Field(default_factory=BehaviorTuning)
    personality: PersonalitySettings = Field(default_factory=PersonalitySettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)

    @classmethod
    def load(cls, path: str | Path | None = None, *, environ: dict[str, str] | None = None) -> RobotConfig:
        """Build the configuration from the hierarchy in the module docstring.

        Never raises for a *missing* file — a deployment with no robot configuration gets
        conservative defaults, which is the right answer. It does raise for a file that
        exists and cannot be read: falling back to defaults after an operator wrote a
        limits file is worse than failing to start.
        """
        env = os.environ if environ is None else environ
        location = Path(path) if path is not None else Path(env.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)
        document = _read_yaml(location) if location.exists() else {}
        if document:
            logger.info("robot configuration loaded from %s", location)
        merged = _with_legacy_files(document)
        merged = _with_environment(merged, env)
        return cls.model_validate(merged)

    def describe(self) -> str:
        """One line per area, for the startup log. No secrets, by construction."""
        return "; ".join(
            (
                f"autonomy={self.robot.autonomy}",
                f"speed<={self.safety.max_speed_mmps}mm/s",
                f"distance<={self.safety.max_distance_mm}mm",
                f"memory={'on ' + self.memory.path if self.memory.enabled else 'off'}",
                f"vision={self.vision.detector if self.vision.enabled else 'off'}",
                f"personality={self.personality.traits.describe()}",
            )
        )


def resolve_secret(value: str | None, *, file_path: str | None = None, env: str | None = None) -> str:
    """A secret from a file, an environment variable or a literal — in that order.

    The order is the point. A container gets `/run/secrets/...`, a developer gets an
    environment variable, and the literal exists so a test can pass one. A file wins over
    a variable because that is the direction a hardened deployment moves in, and a missing
    file is an empty secret rather than an exception: the caller decides whether empty is
    fatal, and for the management API it is — it fails closed.
    """
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("could not read the secret file %s: %s", file_path, exc)
            return ""
    if env:
        from_env = os.environ.get(env, "")
        if from_env:
            return from_env
    return value or ""


# -- the hierarchy ------------------------------------------------------------------------------


def _read_yaml(location: Path) -> dict[str, Any]:
    import yaml  # noqa: PLC0415 - only this function needs it

    with location.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ValueError(f"{location} must contain a mapping, not {type(document).__name__}")
    return dict(document)


def _with_legacy_files(document: dict[str, Any]) -> dict[str, Any]:
    """Fill in a section the main document does not mention from its own old file.

    The migration path, and the reason this is not a breaking change: a deployment that
    already has ``data/robot_limits.yaml`` keeps working, and gets a warning naming the
    section it should move into ``data/robot.yaml``.
    """
    merged = dict(document)
    for section, location in (
        ("safety", DEFAULT_LIMITS_PATH),
        ("behavior", DEFAULT_TUNING_PATH),
    ):
        if section in merged or not location.exists():
            continue
        merged[section] = _read_yaml(location)
        logger.warning(
            "robot configuration: %s was read from the legacy %s; move it under a %r key in %s",
            section,
            location,
            section,
            DEFAULT_CONFIG_PATH,
        )
    if "personality" not in merged and DEFAULT_PERSONALITY_PATH.exists():
        merged["personality"] = {"traits": _read_yaml(DEFAULT_PERSONALITY_PATH)}
        logger.warning(
            "robot configuration: personality traits were read from the legacy %s; move them under "
            "personality.traits in %s",
            DEFAULT_PERSONALITY_PATH,
            DEFAULT_CONFIG_PATH,
        )
    return merged


def _with_environment(document: dict[str, Any], env: dict[str, str] | os._Environ[str]) -> dict[str, Any]:
    """Apply ``NILO_ROBOT_<SECTION>_<FIELD>`` overrides, and ``*_FILE`` secret variables.

    Only fields that already exist on the model are accepted: an unknown variable is a
    warning rather than a silent no-op, because the failure it otherwise produces is
    "I set the speed limit and nothing happened".
    """
    sections = {name: field.annotation for name, field in RobotConfig.model_fields.items()}
    merged = {name: dict(document.get(name) or {}) for name in sections}
    for key, raw in sorted(env.items()):
        if not key.startswith(ENV_PREFIX) or key in FOREIGN_ENV_NAMES:
            continue
        target = _target_field(key[len(ENV_PREFIX) :].lower(), sections)
        if target is None:
            logger.warning("robot configuration: %s names no field; ignoring it", key)
            continue
        section, field, annotation = target
        merged[section][field] = _coerce(raw, annotation)
    return {name: value for name, value in merged.items() if value}


def _target_field(rest: str, sections: dict[str, Any]) -> tuple[str, str, Any] | None:
    """``safety_max_speed_mmps`` -> ``("safety", "max_speed_mmps", int)``.

    A name with no section prefix is looked up in ``robot``, so the runtime settings read
    as ``NILO_ROBOT_AUTONOMY`` rather than as a stutter.
    """
    candidates = [(name, rest[len(name) + 1 :]) for name in sections if rest.startswith(f"{name}_")]
    candidates.append(("robot", rest))
    for section, field in candidates:
        model = sections.get(section)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        declared = model.model_fields.get(field)
        if declared is not None:
            return section, field, declared.annotation
    return None


def _coerce(raw: str, annotation: Any) -> Any:
    """An environment variable is a string; the model wants the type it declared."""
    if annotation is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if annotation is int:
        return int(raw)
    if annotation is float:
        return float(raw)
    return raw


__all__ = [
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "ENV_PREFIX",
    "FOREIGN_ENV_NAMES",
    "MemorySettings",
    "PersonalitySettings",
    "RobotConfig",
    "RuntimeSettings",
    "VisionSettings",
    "resolve_secret",
]
