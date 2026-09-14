"""The scheduler: which behaviour runs, when, and why that one.

One pass (:meth:`BehaviorScheduler.tick`) is four steps and no I/O until the last:

    filter      mode, can_run, cooldown, resource conflicts with what is running
    score       every survivor, plus seeded jitter                     (pure, no awaits)
    arbitrate   rank by (priority band, score); preempt if it is worth it
    execute     start the winner in its own task

Properties that are load-bearing:

* **No LLM.** Nothing in this module, or in anything it calls, reaches a model. The engine
  runs identically with every LLM provider hard-failing (docs/robot-roadmap.md Phase 6).
* **Deterministic.** Given the same world, the same seed and the same tick number, the
  same behaviour is selected — every time, on every machine. Randomness comes only from
  :class:`random.Random` seeded from ``(seed, tick, name)``, never from the wall clock,
  never from dict ordering, never from ``hash()``.
* **Time is injected.** The clock is a callable. Tests pass one they control, so a test
  for "boredom rises over five minutes" runs in microseconds and never flakes.
* **Every decision is explainable.** The full scoring pass — winners, losers, filtered-out
  candidates and their reasons — is published as :class:`~robot.events.types.BehaviorEvaluated`
  and kept as :attr:`BehaviorScheduler.last_decision`.
* **Safety categorically outranks the rest.** The priority band is compared before the
  utility, so no amount of social score reaches a docking run at 8% battery.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from robot.behavior.base import (
    AutonomyMode,
    Behavior,
    BehaviorCategory,
    BehaviorContext,
    BehaviorMemory,
    BehaviorOutcome,
    BehaviorPriority,
    BehaviorResult,
    Drives,
    NeutralDrives,
    RobotCommands,
)
from robot.behavior.tuning import BehaviorTuning
from robot.events.bus import EventBus
from robot.events.types import (
    BehaviorCompleted,
    BehaviorEvaluated,
    BehaviorInterrupted,
    BehaviorSelected,
    BehaviorStarted,
    RobotEvent,
)
from robot.state.actions import Resource
from robot.state.world import WorldState

logger = logging.getLogger(__name__)

#: How many alternatives a :class:`BehaviorSelected` event carries. Enough to explain a
#: decision, few enough to fit in a log line.
EXPLAIN_ALTERNATIVES = 3


class BehaviorRegistry:
    """The behaviours one robot may choose from, by name.

    A registry per scheduler, not a module-level one: two robots in a process can run
    different behaviour sets (different hardware, different deployment), and a test
    registers exactly the two behaviours it is about.
    """

    def __init__(self, behaviors: Iterable[Behavior] = ()) -> None:
        self._behaviors: dict[str, Behavior] = {}
        for behavior in behaviors:
            self.register(behavior)

    def register(self, behavior: Behavior) -> Behavior:
        """Add a behaviour. A duplicate name is an error, not a silent replacement."""
        if behavior.name in self._behaviors:
            raise ValueError(f"a behaviour named {behavior.name!r} is already registered")
        self._behaviors[behavior.name] = behavior
        return behavior

    def replace(self, behavior: Behavior) -> Behavior:
        """Add or overwrite by name. The deliberate version of what :meth:`register` refuses."""
        self._behaviors[behavior.name] = behavior
        return behavior

    def unregister(self, name: str) -> bool:
        return self._behaviors.pop(name, None) is not None

    def get(self, name: str) -> Behavior | None:
        return self._behaviors.get(name)

    def all(self) -> tuple[Behavior, ...]:
        """Every behaviour, in a stable order. Sorted by name, because iteration order
        must not depend on registration order — that is a determinism hole."""
        return tuple(self._behaviors[name] for name in sorted(self._behaviors))

    def __len__(self) -> int:
        return len(self._behaviors)

    def __contains__(self, name: object) -> bool:
        return name in self._behaviors

    def __repr__(self) -> str:
        return f"<BehaviorRegistry {sorted(self._behaviors)}>"


@dataclass(frozen=True)
class BehaviorCandidate:
    """One behaviour's standing in one evaluation pass."""

    behavior: Behavior
    score: float = 0.0
    jitter: float = 0.0
    eligible: bool = True
    #: Why it was filtered out, when it was. Empty for an eligible candidate.
    rejected_because: str = ""
    reasons: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.behavior.name

    @property
    def total(self) -> float:
        """The score the arbitration actually compares, jitter included."""
        return self.score + self.jitter

    @property
    def rank(self) -> tuple[int, float]:
        """Priority band first, utility second. The categorical override rule, as a key."""
        return (int(self.behavior.priority), self.total)

    def __repr__(self) -> str:
        state = "eligible" if self.eligible else f"rejected:{self.rejected_because}"
        return f"<Candidate {self.name} {self.total:.3f} {state}>"


@dataclass(frozen=True)
class BehaviorDecision:
    """The full record of one evaluation pass. What "why is the robot doing this?" answers."""

    tick: int
    at: float
    mode: AutonomyMode
    selected: str | None
    score: float
    candidates: tuple[BehaviorCandidate, ...]
    preempted: str | None = None
    note: str = ""

    @property
    def eligible(self) -> tuple[BehaviorCandidate, ...]:
        return tuple(c for c in self.candidates if c.eligible)

    @property
    def alternatives(self) -> tuple[BehaviorCandidate, ...]:
        """Everything that was eligible and did not win, best first."""
        return tuple(
            sorted((c for c in self.eligible if c.name != self.selected), key=lambda c: c.rank, reverse=True)
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        for candidate in self.candidates:
            if candidate.name == self.selected:
                return candidate.reasons
        return ()

    def explain(self, limit: int = EXPLAIN_ALTERNATIVES) -> str:
        """A human-readable answer, the shape the design asked for."""
        lines = [f"selected: {self.selected or 'none'}", f"score: {self.score:.2f}", ""]
        alternatives = self.alternatives[:limit]
        if alternatives:
            lines.append("alternatives:")
            lines += [f"  {c.name}: {c.total:.2f}" for c in alternatives]
            lines.append("")
        if self.reasons:
            lines.append("reasons:")
            lines += [f"  {reason}" for reason in self.reasons]
            lines.append("")
        skipped = [c for c in self.candidates if not c.eligible]
        if skipped:
            lines.append("not eligible:")
            lines += [f"  {c.name}: {c.rejected_because}" for c in skipped]
        if self.preempted:
            lines.append(f"preempted: {self.preempted}")
        if self.note:
            lines.append(f"note: {self.note}")
        return "\n".join(lines).rstrip() + "\n"

    def as_dict(self) -> dict[str, Any]:
        """The same thing as JSON-able data, for an API endpoint or a structured log."""
        return {
            "tick": self.tick,
            "at": self.at,
            "mode": self.mode.value,
            "selected": self.selected,
            "score": round(self.score, 4),
            "alternatives": {c.name: round(c.total, 4) for c in self.alternatives},
            "reasons": list(self.reasons),
            "not_eligible": {c.name: c.rejected_because for c in self.candidates if not c.eligible},
            "preempted": self.preempted,
            "note": self.note,
        }


@dataclass
class _Running:
    """The behaviour in flight, and what the scheduler needs to supervise it."""

    behavior: Behavior
    task: asyncio.Task[BehaviorResult]
    started_at: float
    score: float

    @property
    def name(self) -> str:
        return self.behavior.name

    def elapsed(self, now: float) -> float:
        return now - self.started_at


@dataclass
class _Stimulus:
    """The last thing worth reacting to, and when. Fed to the sleep/wake behaviours."""

    at: float | None = None
    kind: str = ""


class BehaviorScheduler:
    """Evaluates behaviours for one robot and runs the winner.

    ``clock`` is the only source of time. ``seed`` is the only source of randomness.
    Between them, a scheduler is a pure function of the worlds it is shown — which is the
    entire reason the tests can be exhaustive rather than hopeful.
    """

    def __init__(
        self,
        robot_id: str,
        robot: RobotCommands,
        *,
        registry: BehaviorRegistry | None = None,
        tuning: BehaviorTuning | None = None,
        mode: AutonomyMode = AutonomyMode.NORMAL,
        events: EventBus | None = None,
        seed: int = 0,
        clock: Callable[[], float] = time.monotonic,
        drives: Drives | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.robot = robot
        # Not `registry or BehaviorRegistry()`: an empty registry is falsy (__len__), and a
        # caller that passes one deserves to keep it rather than to get a fresh one.
        self.registry = BehaviorRegistry() if registry is None else registry
        self.tuning = tuning or BehaviorTuning()
        self.seed = seed
        self._mode = mode
        self._events = events
        self._clock = clock
        self._drives: Drives = drives or NeutralDrives()
        self._memory = BehaviorMemory()
        self._last_run: dict[str, float] = {}
        self._running: _Running | None = None
        self._tick = 0
        self._started_at = clock()
        self._stimulus = _Stimulus()
        self._last_decision: BehaviorDecision | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- configuration ------------------------------------------------------------------------

    @property
    def mode(self) -> AutonomyMode:
        return self._mode

    def set_mode(self, mode: AutonomyMode) -> None:
        """Change what the engine may initiate. Does not cancel what is already running —
        :meth:`cancel_running` does, and :meth:`tick` does it on the next pass."""
        self._mode = mode

    @property
    def drives(self) -> Drives:
        return self._drives

    def set_drives(self, drives: Drives) -> None:
        """Install the internal control variables scoring reads (Phase 5 supplies these)."""
        self._drives = drives

    @property
    def memory(self) -> BehaviorMemory:
        return self._memory

    @property
    def last_decision(self) -> BehaviorDecision | None:
        return self._last_decision

    @property
    def running(self) -> str | None:
        return self._running.name if self._running is not None else None

    @property
    def tick_count(self) -> int:
        return self._tick

    def note_stimulus(self, kind: str = "stimulus") -> None:
        """Tell the engine something happened that is worth waking up for.

        Called by the session seam on a touch, a wake word or a detection. It is a hint
        for the sleep/wake behaviours, not an input to safety.
        """
        self._stimulus = _Stimulus(at=self._clock(), kind=kind)

    # -- one pass --------------------------------------------------------------------------------

    def evaluate(self, world: WorldState) -> BehaviorDecision:
        """Score everything and decide, without running anything. Pure and synchronous.

        Split out from :meth:`tick` because it is the half that has to be deterministic,
        and because "what would it do?" is a question the CLI and the tests ask far more
        often than "do it".
        """
        now = self._clock()
        tick = self._tick
        candidates: list[BehaviorCandidate] = []
        running = self._running
        for behavior in self.registry.all():
            context = self._context(world, now, behavior)
            rejection = self._filter(behavior, context, running)
            if rejection:
                candidates.append(
                    BehaviorCandidate(behavior, eligible=False, rejected_because=rejection)
                )
                continue
            try:
                raw = float(behavior.score(context))
            except Exception:  # a behaviour that cannot score itself is not a candidate
                logger.exception("behaviour %s failed to score; skipping it this tick", behavior.name)
                candidates.append(
                    BehaviorCandidate(behavior, eligible=False, rejected_because="scoring raised")
                )
                continue
            score = min(1.0, max(0.0, raw))
            candidates.append(
                BehaviorCandidate(
                    behavior,
                    score=score,
                    jitter=self._jitter(tick, behavior.name),
                    reasons=context.take_reasons(),
                )
            )

        winner, preempted, note = self._arbitrate(candidates, now)
        decision = BehaviorDecision(
            tick=tick,
            at=now,
            mode=self._mode,
            selected=winner.name if winner else None,
            score=winner.total if winner else 0.0,
            candidates=tuple(sorted(candidates, key=lambda c: (c.eligible, c.rank), reverse=True)),
            preempted=preempted,
            note=note,
        )
        self._last_decision = decision
        return decision

    async def tick(self, world: WorldState) -> BehaviorDecision:
        """One full pass: evaluate, publish, preempt if needed, start the winner."""
        if self._closed:
            raise RuntimeError("the behaviour scheduler is closed")
        self._tick += 1
        await self._reap()
        decision = self.evaluate(world)
        await self._publish(_evaluated_event(self.robot_id, decision))

        if await self._enforce_runtime_bounds(decision):
            return decision
        if decision.selected is None:
            return decision
        if self._running is not None and self._running.name == decision.selected:
            return decision  # already doing it; let it finish
        if self._running is not None:
            await self._interrupt(self._running, reason="preempted", by=decision.selected)

        behavior = self.registry.get(decision.selected)
        if behavior is None:  # pragma: no cover - the registry cannot change mid-tick
            return decision
        await self._start(behavior, decision, world)
        return decision

    async def run(self, world_source: Callable[[], WorldState], *, interval_s: float | None = None) -> None:
        """Tick forever on a timer. The engine's own loop, for a server rather than a test.

        ``world_source`` is called once per tick, so the scheduler always scores the
        newest snapshot without holding a reference to a mutable one.
        """
        period = interval_s or self.tuning.tick_interval_s
        while not self._closed:
            try:
                await self.tick(world_source())
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad tick must not end the robot's autonomy
                logger.exception("robot %s: behaviour tick failed", self.robot_id)
            await asyncio.sleep(period)

    def start(self, world_source: Callable[[], WorldState], *, interval_s: float | None = None) -> None:
        """Run :meth:`run` in a task owned by this scheduler."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(
            self.run(world_source, interval_s=interval_s), name=f"robot-behavior-{self.robot_id}"
        )

    async def cancel_running(self, reason: str = "cancelled") -> None:
        """Stop whatever is running now. Used on an e-stop, a mode change, or shutdown."""
        if self._running is not None:
            await self._interrupt(self._running, reason=reason, by=None)

    async def aclose(self) -> None:
        """Stop the loop and the behaviour in flight. Idempotent."""
        if self._closed:
            return
        self._closed = True
        task, self._loop_task = self._loop_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.cancel_running("scheduler closed")

    # -- filtering and arbitration ---------------------------------------------------------------

    def _filter(self, behavior: Behavior, context: BehaviorContext, running: _Running | None) -> str:
        """Why this behaviour is not a candidate, or ``""`` if it is."""
        if not self._mode.allows(behavior):
            return f"autonomy mode {self._mode.value} does not permit it"
        cooldown = behavior.cooldown(self.tuning)
        if cooldown > 0:
            since = context.since_run(behavior.name)
            if since < cooldown:
                return f"cooling down ({cooldown - since:.1f}s left)"
        if running is not None and running.name != behavior.name and behavior.conflicts_with(running.behavior):
            shared = sorted(r.value for r in behavior.required_resources & running.behavior.required_resources)
            return f"{running.name} holds {', '.join(shared)}"
        try:
            if not behavior.can_run(context):
                return "can_run said no"
        except Exception:
            logger.exception("behaviour %s raised in can_run; treating it as ineligible", behavior.name)
            return "can_run raised"
        return ""

    def _arbitrate(
        self, candidates: Sequence[BehaviorCandidate], now: float
    ) -> tuple[BehaviorCandidate | None, str | None, str]:
        """Pick the winner. Returns ``(winner, preempted_name, note)``.

        The rules, in order:

        1. A candidate below ``min_score`` is not worth doing.
        2. Higher priority band wins outright — this is the safety/system override.
        3. Within a band, higher utility wins.
        4. A challenger only preempts a *running* behaviour if the running one is
           interruptible, has had its minimum runtime, and is beaten by more than
           ``preemption_margin`` (or is outranked on band, which needs no margin).
        """
        eligible = [c for c in candidates if c.eligible and c.total >= self.tuning.min_score]
        if not eligible:
            return None, None, "nothing scored above the floor"
        best = max(eligible, key=lambda c: (c.rank, c.name))
        running = self._running
        if running is None:
            return best, None, ""
        if best.name == running.name:
            return best, None, "already running"

        current = next((c for c in eligible if c.name == running.name), None)
        elapsed = running.elapsed(now)
        outranks = int(best.behavior.priority) > int(running.behavior.priority)
        if not running.behavior.interruptible and not outranks:
            return current or best, None, f"{running.name} is not interruptible"
        if elapsed < running.behavior.min_runtime(self.tuning) and not outranks:
            return current or best, None, f"{running.name} is inside its minimum runtime"
        if not outranks:
            margin = best.total - (current.total if current else 0.0)
            if margin < self.tuning.preemption_margin:
                return current or best, None, f"{best.name} does not beat {running.name} by enough"
        return best, running.name, ""

    # -- execution --------------------------------------------------------------------------------

    async def _start(self, behavior: Behavior, decision: BehaviorDecision, world: WorldState) -> None:
        context = self._context(world, decision.at, behavior)
        context.running = behavior.name
        await self._publish(
            BehaviorSelected(
                robot_id=self.robot_id,
                behavior=behavior.name,
                score=decision.score,
                category=behavior.category.value,
                priority=int(behavior.priority),
                alternatives=tuple(
                    (c.name, round(c.total, 4)) for c in decision.alternatives[:EXPLAIN_ALTERNATIVES]
                ),
                reasons=decision.reasons,
                preempted=decision.preempted,
            )
        )
        task: asyncio.Task[BehaviorResult] = asyncio.create_task(
            self._execute(behavior, context), name=f"robot-behavior-{self.robot_id}-{behavior.name}"
        )
        self._running = _Running(behavior, task, decision.at, decision.score)
        await self._publish(
            BehaviorStarted(
                robot_id=self.robot_id,
                behavior=behavior.name,
                score=decision.score,
                resources=tuple(sorted(r.value for r in behavior.required_resources)),
            )
        )

    async def _execute(self, behavior: Behavior, context: BehaviorContext) -> BehaviorResult:
        try:
            return await behavior.execute(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("robot %s: behaviour %s failed", self.robot_id, behavior.name)
            return BehaviorResult.failed(f"{type(exc).__name__}: {exc}")

    async def _reap(self) -> None:
        """Retire the running behaviour if its task has finished."""
        running = self._running
        if running is None or not running.task.done():
            return
        self._running = None
        now = self._clock()
        self._last_run[running.name] = now
        try:
            result = running.task.result()
        except asyncio.CancelledError:
            result = BehaviorResult(BehaviorOutcome.CANCELLED, "cancelled")
        except Exception as exc:  # pragma: no cover - _execute already catches
            result = BehaviorResult.failed(f"{type(exc).__name__}: {exc}")
        await self._publish(
            BehaviorCompleted(
                robot_id=self.robot_id,
                behavior=running.name,
                outcome=result.outcome.value,
                detail=result.detail,
                duration_s=round(running.elapsed(now), 4),
            )
        )

    async def _enforce_runtime_bounds(self, decision: BehaviorDecision) -> bool:
        """Cancel a behaviour that has overrun its maximum. Returns whether it did."""
        running = self._running
        if running is None:
            return False
        if running.elapsed(decision.at) < running.behavior.max_runtime(self.tuning):
            return False
        await self._interrupt(running, reason="exceeded its maximum runtime", by=None)
        return True

    async def _interrupt(self, running: _Running, *, reason: str, by: str | None) -> None:
        now = self._clock()
        self._running = None
        self._last_run[running.name] = now
        with contextlib.suppress(Exception):
            await running.behavior.cancel()
        if not running.task.done():
            running.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await running.task
        await self._publish(
            BehaviorInterrupted(
                robot_id=self.robot_id,
                behavior=running.name,
                reason=reason,
                by=by,
                duration_s=round(running.elapsed(now), 4),
            )
        )

    # -- helpers -------------------------------------------------------------------------------------

    def _context(self, world: WorldState, now: float, behavior: Behavior) -> BehaviorContext:
        return BehaviorContext(
            robot_id=self.robot_id,
            world=world,
            now=now,
            mode=self._mode,
            tuning=self.tuning,
            robot=self.robot,
            rng=random.Random(f"{self.seed}:{self._tick}:{behavior.name}"),
            drives=self._drives,
            memory=self._memory,
            last_run=self._last_run,
            running=self.running,
            started_at=self._started_at,
            last_stimulus_at=self._stimulus.at,
        )

    def _jitter(self, tick: int, name: str) -> float:
        """Seeded variation, so equal options are not always resolved the same way.

        Derived from ``(seed, tick, name)`` and nothing else: no wall clock, no process
        entropy, no ``hash()`` (which is salted per process). ``score_jitter = 0`` turns
        it off entirely for a test that wants bare utilities.
        """
        amplitude = self.tuning.score_jitter
        if amplitude <= 0:
            return 0.0
        return random.Random(f"{self.seed}:{tick}:{name}:jitter").uniform(-amplitude, amplitude)

    async def _publish(self, event: RobotEvent) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(event)
        except Exception as exc:  # a closed bus must not break the engine
            logger.debug("robot %s: could not publish %s: %s", self.robot_id, event.name, exc)

    def __repr__(self) -> str:
        return (
            f"<BehaviorScheduler {self.robot_id} mode={self._mode.value} "
            f"behaviors={len(self.registry)} running={self.running}>"
        )


def _evaluated_event(robot_id: str, decision: BehaviorDecision) -> BehaviorEvaluated:
    return BehaviorEvaluated(
        robot_id=robot_id,
        tick=decision.tick,
        mode=decision.mode.value,
        scores=tuple(
            (c.name, round(c.total, 4))
            for c in sorted(decision.eligible, key=lambda c: c.rank, reverse=True)
        ),
        rejected=tuple((c.name, c.rejected_because) for c in decision.candidates if not c.eligible),
        reasons=tuple((c.name, c.reasons) for c in decision.candidates if c.reasons),
        selected=decision.selected,
    )


def resource_names(resources: Iterable[Resource]) -> tuple[str, ...]:
    return tuple(sorted(resource.value for resource in resources))


__all__ = [
    "EXPLAIN_ALTERNATIVES",
    "BehaviorCandidate",
    "BehaviorCategory",
    "BehaviorDecision",
    "BehaviorPriority",
    "BehaviorRegistry",
    "BehaviorScheduler",
    "resource_names",
]
