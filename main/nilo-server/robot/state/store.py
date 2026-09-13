"""Where robot state lives.

The store is deliberately narrow and fully async so the in-memory implementation can be
swapped for Redis or Postgres without touching a caller: every method is a coroutine,
every value is a serializable pydantic model, and the only compound operation
(:meth:`RobotStateStore.mutate`) is expressed as read-modify-write under a per-robot
lock, which maps onto ``WATCH``/``MULTI`` or ``SELECT ... FOR UPDATE``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable

from robot.state.models import RobotState


class RobotStateStore(ABC):
    """Persistence boundary for :class:`~robot.state.models.RobotState`."""

    @abstractmethod
    async def get(self, robot_id: str) -> RobotState | None:
        """The stored state, or ``None`` if this robot was never registered."""

    @abstractmethod
    async def put(self, state: RobotState) -> None:
        """Store (replacing) the state of one robot."""

    @abstractmethod
    async def mutate(self, robot_id: str, change: Callable[[RobotState], RobotState]) -> RobotState | None:
        """Apply ``change`` atomically. Returns the new state, or ``None`` if absent.

        ``change`` must be a pure function of the current state: an implementation is
        allowed to call it more than once (an optimistic-concurrency retry).
        """

    @abstractmethod
    async def delete(self, robot_id: str) -> bool:
        """Remove a robot. Returns whether anything was removed."""

    @abstractmethod
    async def list_states(self) -> list[RobotState]:
        """Every stored state. Ordering is unspecified.

        Named ``list_states`` and not ``list`` so the class body can still spell the
        builtin ``list[...]`` in its own annotations.
        """

    async def ids(self) -> list[str]:
        return [state.robot_id for state in await self.list_states()]


class InMemoryRobotStateStore(RobotStateStore):
    """Process-local store. No persistence across a restart, no sharing across processes.

    Constructed per runtime (and per test) — there is no module-level instance, because a
    shared dict is exactly the state that makes the inherited server hard to test.
    """

    def __init__(self) -> None:
        self._states: dict[str, RobotState] = {}
        self._lock = asyncio.Lock()

    async def get(self, robot_id: str) -> RobotState | None:
        async with self._lock:
            return self._states.get(robot_id)

    async def put(self, state: RobotState) -> None:
        async with self._lock:
            self._states[state.robot_id] = state

    async def mutate(self, robot_id: str, change: Callable[[RobotState], RobotState]) -> RobotState | None:
        # ponytail: one store-wide lock. Shard per robot id if a hundred robots contend.
        async with self._lock:
            current = self._states.get(robot_id)
            if current is None:
                return None
            updated = change(current)
            self._states[robot_id] = updated
            return updated

    async def delete(self, robot_id: str) -> bool:
        async with self._lock:
            return self._states.pop(robot_id, None) is not None

    async def list_states(self) -> list[RobotState]:
        async with self._lock:
            return list(self._states.values())
