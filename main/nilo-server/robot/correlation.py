"""One identifier from an utterance to a device response.

    from robot.correlation import correlate, current_correlation_id

    with correlate() as trace:          # a fresh id, or keep the one already in scope
        await loop.on_utterance("come closer")

Every :class:`~robot.events.types.RobotEvent` stamps :func:`current_correlation_id` into
its envelope when it is constructed, and every :class:`~robot.state.actions.ActionRecord`
carries the id the action was submitted under. That is the whole mechanism: nothing has
to pass a trace id through a signature, and a subsystem that knows nothing about tracing
still produces correlated events.

The value lives in a :class:`~contextvars.ContextVar`, so it follows the natural shape of
the work: :func:`asyncio.create_task` copies the context, which means the voice turn task,
the agent's tool calls and the executor's submission all inherit the id of the utterance
that started them. The two places it does **not** follow on its own are the ones where a
different task picks the work up later — the action pump and a device notification
arriving on the read loop — and both restore it from the action record instead
(``robot/actions/executor.py``).

An id is 32 hex characters, the same shape as every other identifier in the subsystem
(``event_id``, ``action_id``, ``turn_id``), so a log line does not need to say which kind
of id it is holding for a human to grep it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from uuid import uuid4

#: The id in scope, or ``""`` when nothing is being traced. Never read directly; the
#: accessors below exist so a future change of representation is one file.
_CORRELATION_ID: ContextVar[str] = ContextVar("nilo_robot_correlation_id", default="")


def new_correlation_id() -> str:
    """A fresh trace id."""
    return uuid4().hex


def current_correlation_id() -> str:
    """The trace id in scope, or ``""``. The default factory of every event envelope."""
    return _CORRELATION_ID.get()


@contextlib.contextmanager
def correlate(correlation_id: str | None = None) -> Iterator[str]:
    """Run a block under a trace id, and restore whatever was in scope afterwards.

    ``correlate()`` starts a new trace *unless one is already in scope*, which is what
    makes it safe to wrap an entry point that is sometimes called from inside another
    trace: a behaviour that speaks during a conversation stays on the conversation's
    trace rather than starting a second one. Pass an explicit id to join a trace that
    began elsewhere, or ``""`` to deliberately run untraced.
    """
    if correlation_id is None:
        correlation_id = current_correlation_id() or new_correlation_id()
    token = _CORRELATION_ID.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _CORRELATION_ID.reset(token)


__all__ = ["correlate", "current_correlation_id", "new_correlation_id"]
