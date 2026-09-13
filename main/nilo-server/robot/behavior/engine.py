"""The behaviour engine: one scheduler per robot, wired to the world model.

    engine = BehaviorEngine("nilo-sim-01", runtime.robot("nilo-sim-01"), runtime.world)
    engine.start()                      # ticks on its own timer
    print(engine.explain())             # why is the robot doing this?

This is the assembly, not the mechanism — :mod:`robot.behavior.scheduler` holds the
arbitration and :mod:`robot.behavior.builtins` the behaviours. What lives here is the
wiring that would otherwise be copied into the runtime, the CLI and every test: pull the
newest world snapshot, tick the scheduler with it, and keep the last decision reachable.

The engine never touches an LLM. It is the layer that keeps a robot alive when the model
is down, the network is out, or the deployment has no model at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from robot.behavior.base import AutonomyMode, Behavior, Drives, RobotCommands
from robot.behavior.builtins import default_behaviors
from robot.behavior.scheduler import BehaviorDecision, BehaviorRegistry, BehaviorScheduler
from robot.behavior.tuning import BehaviorTuning
from robot.events.bus import EventBus
from robot.state.world import WorldState

logger = logging.getLogger(__name__)


class WorldSource(Protocol):
    """The slice of :class:`~robot.state.world_model.WorldModel` the engine needs.

    A structural type so a test can pass a one-line fake, and so the engine does not care
    whether the world is fed by the event bus, by a simulator, or by a JSON file on disk.
    """

    def snapshot(self, robot_id: str) -> WorldState: ...


class StaticWorld:
    """A world that never changes. For the CLI, for tests, and for scoring a what-if."""

    def __init__(self, world: WorldState) -> None:
        self.world = world

    def snapshot(self, robot_id: str) -> WorldState:
        return self.world


class BehaviorEngine:
    """Autonomy for one robot: the scheduler, the behaviour set and the world it reads."""

    def __init__(
        self,
        robot_id: str,
        robot: RobotCommands,
        world: WorldSource,
        *,
        events: EventBus | None = None,
        tuning: BehaviorTuning | None = None,
        mode: AutonomyMode = AutonomyMode.NORMAL,
        seed: int = 0,
        clock: Callable[[], float] = time.monotonic,
        behaviors: Iterable[Behavior] | None = None,
        drives: Drives | None = None,
    ) -> None:
        self.robot_id = robot_id
        self._world = world
        registry = BehaviorRegistry(default_behaviors() if behaviors is None else behaviors)
        self.scheduler = BehaviorScheduler(
            robot_id,
            robot,
            registry=registry,
            tuning=tuning or BehaviorTuning(),
            mode=mode,
            events=events,
            seed=seed,
            clock=clock,
            drives=drives,
        )

    # -- configuration ---------------------------------------------------------------------

    @property
    def mode(self) -> AutonomyMode:
        return self.scheduler.mode

    async def set_mode(self, mode: AutonomyMode) -> None:
        """Change the autonomy mode, cancelling anything the new mode would not have started.

        Asynchronous because lowering the mode has to stop what is running — a robot put
        into ``PASSIVE`` while it is driving must not keep driving.
        """
        previous = self.scheduler.mode
        self.scheduler.set_mode(mode)
        if mode.rank >= previous.rank:
            return
        running = self.scheduler.running
        if running is None:
            return
        behavior = self.scheduler.registry.get(running)
        if behavior is not None and not mode.allows(behavior):
            await self.scheduler.cancel_running(f"autonomy mode lowered to {mode.value}")

    @property
    def registry(self) -> BehaviorRegistry:
        return self.scheduler.registry

    @property
    def tuning(self) -> BehaviorTuning:
        return self.scheduler.tuning

    def set_drives(self, drives: Drives) -> None:
        self.scheduler.set_drives(drives)

    def note_stimulus(self, kind: str = "stimulus") -> None:
        self.scheduler.note_stimulus(kind)

    # -- running ----------------------------------------------------------------------------

    def world(self) -> WorldState:
        return self._world.snapshot(self.robot_id)

    def evaluate(self) -> BehaviorDecision:
        """Score everything against the newest snapshot without running anything."""
        return self.scheduler.evaluate(self.world())

    async def tick(self) -> BehaviorDecision:
        """One full pass against the newest snapshot."""
        return await self.scheduler.tick(self.world())

    def start(self, *, interval_s: float | None = None) -> None:
        self.scheduler.start(self.world, interval_s=interval_s)

    async def aclose(self) -> None:
        await self.scheduler.aclose()

    # -- explaining ---------------------------------------------------------------------------

    @property
    def last_decision(self) -> BehaviorDecision | None:
        return self.scheduler.last_decision

    def explain(self, *, fresh: bool = False) -> str:
        """Why the robot is doing what it is doing, as text.

        ``fresh=True`` re-scores against the current world instead of reporting the last
        real decision. Useful from a CLI; misleading from an incident review, which wants
        the decision that actually happened.
        """
        decision = self.evaluate() if fresh else self.last_decision
        if decision is None:
            return "no decision yet\n"
        return decision.explain()

    def explain_data(self, *, fresh: bool = False) -> dict[str, Any]:
        """The same answer as JSON-able data, for an API endpoint."""
        decision = self.evaluate() if fresh else self.last_decision
        return {"robot_id": self.robot_id} | (decision.as_dict() if decision else {"selected": None})

    def __repr__(self) -> str:
        return f"<BehaviorEngine {self.robot_id} mode={self.mode.value} running={self.scheduler.running}>"


__all__ = ["BehaviorEngine", "StaticWorld", "WorldSource"]
