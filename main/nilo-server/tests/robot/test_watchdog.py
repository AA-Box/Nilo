"""The supervisory watchdog: deadlines, sweeps, and the thread it refuses to share.

Most of this file drives :meth:`Watchdog.tick` with an explicit time, so the assertions
are exact rather than timing-dependent. Two tests start the real thread, because the whole
point of the class is that it is *not* on the event loop, and a claim like that has to be
checked rather than asserted in a comment.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from robot.safety.watchdog import Watchdog


@pytest.fixture
def watchdog() -> Watchdog:
    dog = Watchdog(interval_s=0.01)
    yield dog
    dog.stop()


# -- deadlines ---------------------------------------------------------------------------------


def test_a_deadline_fires_once_when_it_passes(watchdog) -> None:
    fired: list[str] = []
    watchdog.arm("action-1", 5.0, fired.append, now=100.0)
    assert watchdog.tick(now=104.9) == ()
    assert fired == []
    assert watchdog.tick(now=105.0) == ("action-1",)
    assert fired == ["action-1"]
    assert watchdog.tick(now=200.0) == ()  # and never again
    assert fired == ["action-1"]


def test_disarming_prevents_the_callback(watchdog) -> None:
    fired: list[str] = []
    watchdog.arm("action-1", 1.0, fired.append, now=0.0)
    assert watchdog.disarm("action-1") is True
    assert watchdog.tick(now=10.0) == ()
    assert fired == []


def test_disarming_something_that_was_never_armed_is_a_no_op(watchdog) -> None:
    """The executor disarms on every settle, including for actions that never ran."""
    assert watchdog.disarm("nothing") is False
    assert watchdog.disarm("nothing") is False


def test_re_arming_replaces_the_deadline(watchdog) -> None:
    fired: list[str] = []
    watchdog.arm("action-1", 1.0, fired.append, now=0.0)
    watchdog.arm("action-1", 100.0, fired.append, now=0.0)
    assert watchdog.tick(now=50.0) == ()
    assert watchdog.deadline("action-1") == 100.0
    assert watchdog.tick(now=100.0) == ("action-1",)


def test_several_deadlines_fire_together(watchdog) -> None:
    fired: list[str] = []
    for index, timeout in enumerate((1.0, 2.0, 30.0)):
        watchdog.arm(f"action-{index}", timeout, fired.append, now=0.0)
    assert set(watchdog.tick(now=5.0)) == {"action-0", "action-1"}
    assert set(fired) == {"action-0", "action-1"}
    assert watchdog.armed() == ("action-2",)


def test_a_callback_that_raises_does_not_stop_the_others(watchdog) -> None:
    """A watchdog that stops supervising because one cleanup failed is worse than none:
    it still looks like a watchdog."""
    fired: list[str] = []

    def explode(key: str) -> None:
        raise RuntimeError("cleanup failed")

    watchdog.arm("bad", 1.0, explode, now=0.0)
    watchdog.arm("good", 1.0, fired.append, now=0.0)
    assert set(watchdog.tick(now=2.0)) == {"bad", "good"}
    assert fired == ["good"]


def test_a_callback_may_arm_another_deadline_without_deadlocking(watchdog) -> None:
    """Callbacks run outside the lock, which is what makes a re-arm from inside safe."""
    fired: list[str] = []

    def rearm(key: str) -> None:
        fired.append(key)
        if key == "first":
            watchdog.arm("second", 1.0, rearm, now=10.0)

    watchdog.arm("first", 1.0, rearm, now=0.0)
    watchdog.tick(now=10.0)
    watchdog.tick(now=11.0)
    assert fired == ["first", "second"]


def test_a_zero_or_negative_timeout_is_due_immediately(watchdog) -> None:
    fired: list[str] = []
    watchdog.arm("now", 0.0, fired.append, now=5.0)
    watchdog.arm("past", -10.0, fired.append, now=5.0)
    assert set(watchdog.tick(now=5.0)) == {"now", "past"}


# -- sweeps -------------------------------------------------------------------------------------------


def test_a_sweep_runs_on_its_own_interval(watchdog) -> None:
    """This is where "stop motion when the heartbeat expires" lives: nothing is armed
    against a heartbeat, so something has to look."""
    runs: list[int] = []
    watchdog.add_sweep(lambda: runs.append(1), 1.0, now=0.0)
    watchdog.tick(now=0.5)
    assert runs == []
    watchdog.tick(now=1.0)
    watchdog.tick(now=1.5)
    watchdog.tick(now=2.0)
    assert len(runs) == 2


def test_a_sweep_that_raises_is_logged_and_keeps_its_schedule(watchdog) -> None:
    runs: list[int] = []

    def flaky() -> None:
        runs.append(len(runs))
        raise RuntimeError("sweep failed")

    watchdog.add_sweep(flaky, 1.0, now=0.0)
    watchdog.tick(now=1.0)
    watchdog.tick(now=2.0)
    assert len(runs) == 2


def test_a_non_positive_sweep_interval_is_refused(watchdog) -> None:
    with pytest.raises(ValueError):
        watchdog.add_sweep(lambda: None, 0.0)


def test_a_non_positive_tick_interval_is_refused() -> None:
    with pytest.raises(ValueError):
        Watchdog(interval_s=0.0)


# -- the thread ----------------------------------------------------------------------------------------------


def test_the_watchdog_runs_on_a_thread_of_its_own(watchdog) -> None:
    """Not the event loop, on purpose: Silero inference and the GC pass both live there,
    and a timer that fires when the loop gets round to it is not a deadline."""
    seen: list[str] = []
    watchdog.arm("action-1", 0.02, lambda key: seen.append(threading.current_thread().name))
    watchdog.start()
    deadline = time.monotonic() + 3.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen, "the watchdog thread never fired the deadline"
    assert seen[0] != threading.current_thread().name
    assert seen[0].startswith("robot-watchdog")


async def test_a_deadline_still_fires_while_the_event_loop_is_blocked(watchdog) -> None:
    """The property the whole design rests on, demonstrated rather than asserted.

    The loop is blocked in a synchronous ``time.sleep`` — the closest a test can get to a
    GC pause or an ONNX inference — and the deadline still fires while it is stuck.
    """
    fired = threading.Event()
    watchdog.arm("action-1", 0.05, lambda key: fired.set())
    watchdog.start()
    time.sleep(0.4)  # the event loop cannot run anything at all during this
    assert fired.is_set()
    await asyncio.sleep(0)


def test_start_is_idempotent_and_stop_clears_everything(watchdog) -> None:
    watchdog.start()
    watchdog.start()
    assert watchdog.running
    watchdog.arm("action-1", 100.0, lambda key: None)
    watchdog.add_sweep(lambda: None, 100.0)
    watchdog.stop()
    assert not watchdog.running
    assert watchdog.armed() == ()
    watchdog.stop()  # twice is fine


def test_stopping_one_that_never_started_is_safe() -> None:
    Watchdog().stop()
