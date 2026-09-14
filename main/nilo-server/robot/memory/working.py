"""Working memory: what is happening right now, and nothing older.

The shortest-lived of the four stores, and the only one that is not persisted. It holds
the current conversation and the handful of things the robot is currently paying attention
to, it is bounded, and it expires.

Why it is a separate thing rather than "recent episodic memory": the two have different
lifetimes, different sizes and different truth. Working memory is allowed to hold a
half-finished sentence and a guess about who is talking; episodic memory is what the robot
will still believe tomorrow. Merging them means either persisting noise or losing context.

Everything here is synchronous and in-process. There is nothing to await, nothing to
migrate, and nothing to delete on request beyond dropping the object.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

#: How many turns are kept. Beyond this the oldest is dropped, because a working memory
#: that grows without bound is a prompt that grows without bound.
DEFAULT_MAX_TURNS = 12

#: How long a turn stays relevant, in seconds. A conversation that stopped ten minutes ago
#: is not the current conversation.
DEFAULT_TTL_S = 600.0


@dataclass(frozen=True)
class WorkingItem:
    """One thing in working memory: a turn, an observation, a note."""

    text: str
    at: float
    role: str = "user"
    person_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def age_s(self, now: float) -> float:
        return now - self.at

    @property
    def line(self) -> str:
        who = self.person_id or self.role
        return f"{who}: {self.text}"


class WorkingMemory:
    """A bounded, expiring buffer of the current interaction.

    ``clock`` is injected like everywhere else in the subsystem, so a test for "a turn from
    eleven minutes ago is not current" runs instantly and never flakes.
    """

    def __init__(
        self,
        robot_id: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.robot_id = robot_id
        self.max_turns = max_turns
        self.ttl_s = ttl_s
        self._clock = clock
        self._items: deque[WorkingItem] = deque(maxlen=max_turns)

    def add(self, text: str, *, role: str = "user", person_id: str | None = None, **metadata: Any) -> WorkingItem:
        item = WorkingItem(
            text=text.strip(), at=self._clock(), role=role, person_id=person_id, metadata=metadata
        )
        self._items.append(item)
        return item

    def items(self) -> tuple[WorkingItem, ...]:
        """Everything still inside the TTL, oldest first. Expired entries are dropped here."""
        now = self._clock()
        while self._items and self._items[0].age_s(now) > self.ttl_s:
            self._items.popleft()
        return tuple(self._items)

    def recent(self, limit: int | None = None) -> tuple[WorkingItem, ...]:
        current = self.items()
        return current if limit is None else current[-limit:]

    def transcript(self, limit: int | None = None) -> str:
        """The current exchange as text. What goes into a prompt, budget permitting."""
        return "\n".join(item.line for item in self.recent(limit))

    @property
    def current_person(self) -> str | None:
        """Who the robot is most recently talking to, if anybody."""
        for item in reversed(self.items()):
            if item.person_id:
                return item.person_id
        return None

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self.items())

    def __iter__(self) -> Iterator[WorkingItem]:
        return iter(self.items())

    def __repr__(self) -> str:
        return f"<WorkingMemory {self.robot_id} {len(self._items)}/{self.max_turns} turns>"


def as_lines(items: Iterable[WorkingItem]) -> list[str]:
    return [item.line for item in items]


__all__ = ["DEFAULT_MAX_TURNS", "DEFAULT_TTL_S", "WorkingItem", "WorkingMemory", "as_lines"]
