"""The composition root: one function that turns configuration into a running subsystem.

    from robot.bootstrap import start_robot_subsystem

    runtime = await start_robot_subsystem()   # called once, from app.py

Everything under ``robot/`` is built to be constructed rather than discovered: the runtime
takes its limits, its stores and its provider as arguments, and nothing reaches for a
module-level singleton. That is the right design and it had one consequence nobody had
closed — **nothing assembled it**. ``get_runtime()`` built a bare :class:`RobotRuntime`,
so a server read no limits file, opened no memory database, persisted no personality and
never installed a model. Every one of those subsystems worked, was tested, and was
unreachable from ``app.py``.

This module is that assembly, and it is the only place that knows the whole graph:

    RobotConfig ──▶ SafetyLimits ──▶ RobotActionExecutor
                ├─▶ SqliteMemoryStore
                ├─▶ PersonalityStore
                ├─▶ autonomy mode
                └─▶ per-robot behaviour and vision loops, on connect

It imports nothing from ``core/``: the language model arrives through the session seam,
per connection, which is where the inherited server already decides which provider a
device gets (``robot/voice/seam.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from robot.config import RobotConfig
from robot.events.types import RobotConnected
from robot.runtime import RobotRuntime, set_runtime

logger = logging.getLogger(__name__)


def build_runtime(settings: RobotConfig | None = None) -> RobotRuntime:
    """A runtime configured from ``settings``. Does not install it, and opens nothing.

    Stores are constructed, not opened: the SQLite connection is made on first use, which
    is also when migrations run, and a process that never sees a robot should not create a
    database file by starting.
    """
    active = settings or RobotConfig.load()
    memory_store = None
    if active.memory.enabled:
        from robot.memory.store import SqliteMemoryStore  # noqa: PLC0415 - optional subsystem

        memory_store = SqliteMemoryStore(active.memory.path)
    personality_store = None
    if active.personality.store_dir:
        from robot.personality.store import PersonalityStore  # noqa: PLC0415

        personality_store = PersonalityStore(Path(active.personality.store_dir))

    runtime = RobotRuntime(
        discovery_timeout=active.robot.discovery_timeout_s,
        limits=active.safety,
        memory_store=memory_store,
        personality_store=personality_store,
        settings=active,
    )
    return runtime


async def start_robot_subsystem(settings: RobotConfig | None = None) -> RobotRuntime:
    """Build the runtime, install it as the process-wide one, and wire the per-robot loops.

    Returns the runtime so a caller can close it. Never raises for a configuration problem
    it can survive: a memory database it cannot open, or an animation directory that is not
    there, is a warning and a subsystem that stays off — a robot that will not accept a
    session because a YAML file moved is a worse failure than one with no long-term memory.
    """
    active = settings or RobotConfig.load()
    runtime = build_runtime(active)
    set_runtime(runtime)
    _attach_per_robot_loops(runtime, active)
    await _set_autonomy(runtime, active)
    logger.info("robot subsystem started: %s", active.describe())
    return runtime


def _attach_per_robot_loops(runtime: RobotRuntime, settings: RobotConfig) -> None:
    """Start the behaviour and vision loops for each robot as it connects.

    A subscriber rather than a hook in ``robot/session.py``: the session seam's job is to
    register a device, and "and also start deciding what it should do" is a deployment
    policy. A deployment that wants a robot to sit still until an operator says otherwise
    leaves both of these off, which is the default.
    """
    if not settings.robot.autostart_behaviors and not settings.vision.enabled:
        return

    def on_connect(event: RobotConnected) -> None:
        robot_id = event.robot_id
        if settings.robot.autostart_behaviors:
            try:
                runtime.behavior(robot_id).start(interval_s=settings.robot.behavior_interval_s)
                logger.info("robot %s: behaviour engine started", robot_id)
            except Exception as exc:  # one robot's engine must not stop the next one's
                logger.warning("robot %s: could not start the behaviour engine: %s", robot_id, exc)
        if settings.vision.enabled:
            try:
                pipeline = runtime.vision(robot_id, detector=_detector(settings))
                pipeline.start(interval_s=settings.vision.interval_s)
                logger.info("robot %s: vision started (%s)", robot_id, settings.vision.detector)
            except Exception as exc:
                logger.warning("robot %s: could not start vision: %s", robot_id, exc)

    runtime.events.subscribe(RobotConnected, on_connect)


def _detector(settings: RobotConfig) -> Any:
    """The detector the configuration names, or the null one when it cannot be built.

    A missing model file leaves a pipeline that runs and finds nothing, which is the same
    state a deployment with no model is in. It is a loud warning and a quiet robot, not a
    server that will not start.
    """
    from robot.vision.providers import ColourBlobDetector, NullDetector  # noqa: PLC0415

    choice = settings.vision.detector.strip().lower()
    if choice in ("", "null", "none"):
        return NullDetector()
    if choice in ("colour-blob", "color-blob", "blob"):
        return ColourBlobDetector()
    if choice == "yolo":
        from robot.vision.providers import YoloObjectDetector  # noqa: PLC0415

        try:
            return YoloObjectDetector(settings.vision.model_path or "yolo11n.pt")
        except Exception as exc:
            logger.warning("vision: the YOLO detector could not be built (%s); finding nothing", exc)
            return NullDetector()
    logger.warning("vision: unknown detector %r; finding nothing", settings.vision.detector)
    return NullDetector()


async def _set_autonomy(runtime: RobotRuntime, settings: RobotConfig) -> None:
    from robot.behavior.base import AutonomyMode  # noqa: PLC0415

    wanted = settings.robot.autonomy.strip().lower()
    try:
        mode = AutonomyMode(wanted)
    except ValueError:
        logger.warning(
            "robot configuration: unknown autonomy mode %r; staying in %s",
            settings.robot.autonomy,
            AutonomyMode.NORMAL.value,
        )
        return
    await runtime.set_autonomy(mode)


__all__ = ["build_runtime", "start_robot_subsystem"]
