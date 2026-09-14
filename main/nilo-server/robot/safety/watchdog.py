"""The supervisory watchdog: deadlines, on a thread of their own.

Why a thread and not an ``asyncio`` timer. The session event loop runs Silero VAD
inference inline (``core/handle/receiveAudioHandle.py``), several ASR providers block on
it, and ``core/utils/gc_manager.py`` walks ``gc.get_objects()`` twice every 300 seconds.
A timer scheduled on that loop fires when the loop gets around to it, which is not a
deadline. So this runs on its own thread, with a plain ``threading.Event`` for its sleep,
and never touches an event loop itself.

That thread does not make the watchdog authoritative. The GIL means a long C-level pause
stalls this thread too, and a process that is killed supervises nothing at all. **The
authoritative watchdog is in firmware**, where loss of heartbeat stops the motors with no
round trip. This one is the backend noticing that a device never reported back, so an
action can be marked ``TIMED_OUT`` and a stop can be *attempted*. See docs/safety-model.md.

Callbacks run on the watchdog thread. A caller that needs to touch an event loop supplies
a callback that hops there itself — :class:`~robot.actions.executor.RobotActionExecutor`
passes one built around ``loop.call_soon_threadsafe`` — which keeps the hand-off explicit
at exactly one seam instead of implicit everywhere.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: How often the thread wakes to compare deadlines. 100 ms is two orders of magnitude
#: finer than the shortest action timeout and costs one wakeup per tick.
DEFAULT_INTERVAL_S = 0.1

ExpiryCallback = Callable[[str], None]
SweepCallback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class _Deadline:
    key: str
    at: float
    on_expire: ExpiryCallback


@dataclass
class _Sweep:
    callback: SweepCallback
    interval_s: float
    next_at: float = field(default=0.0)


class Watchdog:
    """Fires a callback when a deadline passes, and runs periodic supervisory sweeps.

    Usable two ways, and the tests use both:

    * **Threaded** — :meth:`start` spawns the thread, :meth:`stop` joins it.
    * **Driven** — never start it, and call :meth:`tick` with an explicit time. The same
      code path fires the same callbacks, deterministically, with no sleeping.
    """

    def __init__(self, *, interval_s: float = DEFAULT_INTERVAL_S, name: str = "robot-watchdog") -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._interval_s = interval_s
        self._name = name
        self._lock = threading.Lock()
        self._deadlines: dict[str, _Deadline] = {}
        self._sweeps: list[_Sweep] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Run the watchdog on its own daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread = thread
        thread.start()
        logger.debug("watchdog %s started at a %.0f ms interval", self._name, self._interval_s * 1000)

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread and wait for it. Idempotent, and safe if never started."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._deadlines.clear()
            self._sweeps.clear()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- deadlines --------------------------------------------------------------------------

    def arm(self, key: str, timeout_s: float, on_expire: ExpiryCallback, *, now: float | None = None) -> float:
        """Fire ``on_expire(key)`` once, ``timeout_s`` from now. Re-arming replaces.

        Returns the absolute deadline, on the same monotonic scale :meth:`tick` takes.
        """
        at = (time.monotonic() if now is None else now) + max(0.0, timeout_s)
        with self._lock:
            self._deadlines[key] = _Deadline(key=key, at=at, on_expire=on_expire)
        return at

    def disarm(self, key: str) -> bool:
        """Cancel a deadline. Returns whether one was armed. Safe to call twice."""
        with self._lock:
            return self._deadlines.pop(key, None) is not None

    def armed(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._deadlines))

    @property
    def sweeps(self) -> int:
        """How many periodic sweeps are registered. Lets a caller assert its own wiring."""
        with self._lock:
            return len(self._sweeps)

    def deadline(self, key: str) -> float | None:
        with self._lock:
            entry = self._deadlines.get(key)
        return entry.at if entry is not None else None

    # -- periodic sweeps ---------------------------------------------------------------------

    def add_sweep(self, callback: SweepCallback, interval_s: float, *, now: float | None = None) -> None:
        """Call ``callback`` every ``interval_s``, for as long as the watchdog runs.

        This is where "stop motion when the heartbeat expires" lives: nothing is armed
        against a heartbeat, so something has to look.
        """
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        start = (time.monotonic() if now is None else now) + interval_s
        with self._lock:
            self._sweeps.append(_Sweep(callback=callback, interval_s=interval_s, next_at=start))

    # -- the tick ------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> tuple[str, ...]:
        """Fire everything due at ``now``. Returns the keys whose deadlines expired.

        Callbacks run outside the lock, so one may re-arm, disarm or add a sweep without
        deadlocking. A callback that raises is logged and the others still run: a
        watchdog that stops supervising because one action's cleanup failed is worse than
        no watchdog, because it looks like one.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            expired = [entry for entry in self._deadlines.values() if entry.at <= moment]
            for entry in expired:
                self._deadlines.pop(entry.key, None)
            due = [sweep for sweep in self._sweeps if sweep.next_at <= moment]
            for sweep in due:
                sweep.next_at = moment + sweep.interval_s
        for entry in expired:
            try:
                entry.on_expire(entry.key)
            except Exception:
                logger.exception("watchdog %s: expiry callback for %s failed", self._name, entry.key)
        for sweep in due:
            try:
                sweep.callback()
            except Exception:
                logger.exception("watchdog %s: sweep callback failed", self._name)
        return tuple(entry.key for entry in expired)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.tick()
            except Exception:  # pragma: no cover - tick already swallows callback failures
                logger.exception("watchdog %s: tick failed", self._name)

    def __repr__(self) -> str:
        return f"<Watchdog {self._name} running={self.running} armed={len(self._deadlines)}>"


__all__ = ["DEFAULT_INTERVAL_S", "ExpiryCallback", "SweepCallback", "Watchdog"]
