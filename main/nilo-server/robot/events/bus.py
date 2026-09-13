"""An in-process, async, bounded event bus.

Properties that are load-bearing (docs/robot-roadmap.md Phase 1):

* **Per-subscriber queues.** A slow subscriber cannot delay a fast one, and cannot delay
  the publisher: :meth:`EventBus.publish` never awaits a handler.
* **Bounded with drop-oldest.** A full queue drops its oldest event and counts the drop.
  The inherited audio queue is unbounded and backlogs silently under load; this does not.
* **Isolated failures.** A handler that raises is logged and its subscription carries on.
* **Clean shutdown.** :meth:`EventBus.aclose` cancels every worker and drains nothing —
  shutdown is not a flush. Call :meth:`EventBus.drain` first if delivery matters.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from robot.events.types import RobotEvent

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=RobotEvent)

EventHandler = Callable[[Any], Awaitable[None] | None]

DEFAULT_QUEUE_SIZE = 256


class Subscription:
    """A handle returned by :meth:`EventBus.subscribe`. Pass it back to unsubscribe."""

    __slots__ = ("_worker", "delivered", "dropped", "event_types", "failed", "handler", "id", "queue")

    def __init__(
        self,
        subscription_id: int,
        event_types: tuple[type[RobotEvent], ...],
        handler: EventHandler,
        queue_size: int,
    ) -> None:
        self.id = subscription_id
        self.event_types = event_types
        self.handler = handler
        self.queue: asyncio.Queue[RobotEvent] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self.delivered = 0
        self.dropped = 0
        self.failed = 0

    def matches(self, event: RobotEvent) -> bool:
        return isinstance(event, self.event_types)

    def __repr__(self) -> str:
        names = "|".join(t.__name__ for t in self.event_types)
        return f"<Subscription {self.id} {names} delivered={self.delivered} dropped={self.dropped}>"


class EventBus:
    """Typed publish/subscribe over the running event loop.

    Constructed per runtime — there is no module-level bus, so a test builds its own and
    two tests never share state.
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self._queue_size = queue_size
        self._subscriptions: dict[int, Subscription] = {}
        self._next_id = 1
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return tuple(self._subscriptions.values())

    @property
    def dropped(self) -> int:
        """Events dropped across every subscription since start."""
        return sum(sub.dropped for sub in self._subscriptions.values())

    def subscribe(
        self,
        event_type: type[E] | tuple[type[RobotEvent], ...],
        handler: Callable[[E], Awaitable[None] | None],
        *,
        queue_size: int | None = None,
    ) -> Subscription:
        """Deliver every event that is an instance of ``event_type`` to ``handler``.

        Must be called with a running event loop: each subscription owns a worker task.
        The handler may be a coroutine function or a plain callable.
        """
        if self._closed:
            raise RuntimeError("event bus is closed")
        types = event_type if isinstance(event_type, tuple) else (event_type,)
        subscription = Subscription(self._next_id, types, handler, queue_size or self._queue_size)
        self._next_id += 1
        self._subscriptions[subscription.id] = subscription
        subscription._worker = asyncio.create_task(
            self._run(subscription), name=f"robot-event-subscriber-{subscription.id}"
        )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> bool:
        """Stop delivering to a subscription. Idempotent; safe from inside a handler."""
        removed = self._subscriptions.pop(subscription.id, None)
        if removed is None:
            return False
        worker = removed._worker
        if worker is not None and not worker.done():
            worker.cancel()
        return True

    async def publish(self, event: RobotEvent) -> None:
        """Hand an event to every matching subscriber. Never blocks on a handler.

        A subscriber whose queue is full loses its oldest undelivered event, not this
        one: the newest robot state is the one worth keeping.
        """
        if self._closed:
            raise RuntimeError("event bus is closed")
        for subscription in list(self._subscriptions.values()):
            if not subscription.matches(event):
                continue
            while True:
                try:
                    subscription.queue.put_nowait(event)
                    break
                except asyncio.QueueFull:
                    try:
                        subscription.queue.get_nowait()
                        subscription.queue.task_done()
                    except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                        pass
                    subscription.dropped += 1
                    logger.warning(
                        "robot event bus: subscription %s is full, dropped the oldest event (%d total)",
                        subscription.id,
                        subscription.dropped,
                    )

    async def drain(self) -> None:
        """Wait until every queued event has been handled. For tests and shutdown."""
        await asyncio.gather(*(sub.queue.join() for sub in list(self._subscriptions.values())))

    async def aclose(self) -> None:
        """Cancel every worker and forget every subscription. Idempotent."""
        self._closed = True
        subscriptions = list(self._subscriptions.values())
        self._subscriptions.clear()
        workers = [sub._worker for sub in subscriptions if sub._worker is not None]
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _run(self, subscription: Subscription) -> None:
        while True:
            event = await subscription.queue.get()
            try:
                result = subscription.handler(event)
                if isinstance(result, Awaitable):
                    await result
                subscription.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                subscription.failed += 1
                logger.exception("robot event subscriber %s failed on %s", subscription.id, type(event).__name__)
            finally:
                subscription.queue.task_done()
