"""Clocks the simulator runs on: real, accelerated, or driven by hand.

Three reasons this is not ``time.monotonic`` plus ``asyncio.sleep``:

* A behaviour that takes two minutes of robot time has to be testable in under a second,
  so the clock carries a ``speed`` multiplier and every wait goes through it.
* A test that asserts "after 3.0 s of motion the pose is here" must not depend on how
  busy the host is, so :class:`ManualClock` advances only when a test says so.
* Simulated time is the only time the physics step sees. Nothing in the simulator reads
  the wall clock directly.

``now()`` is simulated seconds since the clock was created, not an epoch timestamp.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Protocol, runtime_checkable

#: Smallest wait a real clock actually sleeps on. Below this the event loop overhead
#: dominates and a 1 ms sleep costs more than the tick it is pacing.
MIN_REAL_SLEEP_S = 0.0005


@runtime_checkable
class Clock(Protocol):
    """Simulated time, and a way to wait for more of it."""

    @property
    def speed(self) -> float:
        """Simulated seconds per real second. 1.0 is real time."""

    def now(self) -> float:
        """Simulated seconds since this clock started."""

    async def sleep(self, seconds: float) -> None:
        """Wait until ``now()`` has advanced by ``seconds`` of simulated time."""


class RealClock:
    """Wall-clock time, optionally accelerated.

    ``speed=10`` makes a 60 s scenario finish in 6 s: ``now()`` runs ten times faster and
    every :meth:`sleep` is divided by the same factor, so the physics step still sees a
    consistent dt.
    """

    def __init__(self, speed: float = 1.0, *, start: float = 0.0) -> None:
        if speed <= 0:
            raise ValueError("clock speed must be positive")
        self._speed = speed
        self._start = start
        self._origin = time.monotonic()

    @property
    def speed(self) -> float:
        return self._speed

    def now(self) -> float:
        return self._start + (time.monotonic() - self._origin) * self._speed

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        real = seconds / self._speed
        await asyncio.sleep(real if real > MIN_REAL_SLEEP_S else MIN_REAL_SLEEP_S)


class ManualClock:
    """Time that only moves when :meth:`advance` is called.

    Deterministic by construction: the same sequence of ``advance`` calls produces the
    same sequence of physics steps, telemetry frames and scenario steps on every machine.
    Sleepers are released in deadline order, with one event-loop turn between releases so
    the woken task runs before time moves on.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

    @property
    def speed(self) -> float:
        return 1.0

    @property
    def sleeping(self) -> int:
        return len(self._waiters)

    def now(self) -> float:
        return self._t

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._t + seconds, next(self._counter), future))
        await future

    async def advance(self, seconds: float) -> None:
        """Move simulated time forward, releasing every sleeper it passes.

        Releases in deadline order and yields to the loop after each one, so a task that
        loops "sleep, work, sleep" makes the same number of iterations it would on a real
        clock over the same interval.
        """
        if seconds < 0:
            raise ValueError("time does not run backwards")
        target = self._t + seconds
        while self._waiters and self._waiters[0][0] <= target:
            deadline, _, future = heapq.heappop(self._waiters)
            self._t = max(self._t, deadline)
            if not future.done():
                future.set_result(None)
            await asyncio.sleep(0)
        self._t = target
        await asyncio.sleep(0)


def build_clock(speed: float = 1.0, *, manual: bool = False) -> Clock:
    """The clock a CLI run or a test asks for."""
    return ManualClock() if manual else RealClock(speed)
