"""The action queue and the resource ledger that arbitrates between actions.

One structure, two halves:

* **Pending** — per robot, ordered by priority then age. Nothing in here is touching
  hardware yet.
* **Running** — a map from :class:`~robot.state.actions.Resource` to the single action
  that owns it. The map *is* the mutual exclusion: a resource has one entry or none, so
  two actions cannot command the same physical subsystem, whatever the caller intended.

Preemption is a first-class outcome, not a dropped request (docs/robot-architecture.md
Sect. 2.8): when a user says "come here" while a behaviour is looking around, the
behaviour's action is cancelled with an observable transition and the user's action takes
the claim.

This class is not a concurrency boundary. It is driven exclusively from the executor's
dispatch task, which is single-threaded on one event loop; nothing here awaits, so no
interleaving is possible between a conflict check and the claim that follows it.
"""

from __future__ import annotations

from collections.abc import Iterable

from robot.actions.model import RobotAction, sort_key
from robot.state.actions import Resource


class ResourceConflict(RuntimeError):
    """A claim was attempted on a resource another action already owns."""

    def __init__(self, action_id: str, resource: Resource, holder_id: str) -> None:
        super().__init__(f"action {action_id}: {resource.value} is held by {holder_id}")
        self.action_id = action_id
        self.resource = resource
        self.holder_id = holder_id


class RobotActionQueue:
    """Pending actions per robot, and which action owns which subsystem right now."""

    def __init__(self) -> None:
        self._pending: dict[str, list[RobotAction]] = {}
        self._running: dict[str, dict[Resource, RobotAction]] = {}

    # -- pending ---------------------------------------------------------------------------

    def enqueue(self, action: RobotAction) -> None:
        """Add an action to its robot's queue, in dispatch order."""
        queue = self._pending.setdefault(action.robot_id, [])
        queue.append(action)
        queue.sort(key=sort_key)

    def remove(self, action_id: str) -> RobotAction | None:
        """Take one action out of the pending queue. Returns it, or ``None``."""
        for robot_id, queue in self._pending.items():
            for index, action in enumerate(queue):
                if action.action_id == action_id:
                    del queue[index]
                    if not queue:
                        del self._pending[robot_id]
                    return action
        return None

    def pending(self, robot_id: str | None = None) -> tuple[RobotAction, ...]:
        if robot_id is not None:
            return tuple(self._pending.get(robot_id, ()))
        return tuple(action for queue in self._pending.values() for action in queue)

    def depth(self, robot_id: str) -> int:
        return len(self._pending.get(robot_id, ()))

    def robot_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    def drain(self, robot_id: str | None = None) -> tuple[RobotAction, ...]:
        """Remove and return every pending action (for one robot, or all of them).

        What ``cancel_all`` and the emergency stop empty. Running actions are untouched:
        the caller cancels those through their own transitions, because they own claims.
        """
        if robot_id is None:
            drained = tuple(action for queue in self._pending.values() for action in queue)
            self._pending.clear()
            return drained
        return tuple(self._pending.pop(robot_id, []))

    # -- running and claims ------------------------------------------------------------------

    def running(self, robot_id: str | None = None) -> tuple[RobotAction, ...]:
        """Distinct actions holding at least one resource. Stable order by action id."""
        if robot_id is not None:
            holders = self._running.get(robot_id, {})
            return tuple(sorted(set(holders.values()), key=lambda action: action.action_id))
        actions = {action for holders in self._running.values() for action in holders.values()}
        return tuple(sorted(actions, key=lambda action: action.action_id))

    def holder(self, robot_id: str, resource: Resource) -> RobotAction | None:
        return self._running.get(robot_id, {}).get(resource)

    def conflicts(self, action: RobotAction) -> tuple[RobotAction, ...]:
        """The running actions that would have to release a resource for this one to run.

        The conflict-detection primitive: it answers "what is in the way", not merely
        "is something in the way", so a rejection or a preemption can name the holder.
        """
        holders = self._running.get(action.robot_id, {})
        blocking = {holders[resource].action_id: holders[resource] for resource in action.resources if resource in holders}
        return tuple(sorted(blocking.values(), key=lambda held: held.action_id))

    def is_free(self, action: RobotAction) -> bool:
        return not self.conflicts(action)

    def preemptable(self, action: RobotAction) -> tuple[RobotAction, ...]:
        """Blocking actions this one outranks. Empty means it must wait, not preempt.

        Strictly greater, so two actions at the same priority queue behind each other
        instead of cancelling each other in a loop.
        """
        return tuple(held for held in self.conflicts(action) if int(held.priority) < int(action.priority))

    def claim(self, action: RobotAction) -> None:
        """Take every resource this action needs. Raises if any of them is held."""
        holders = self._running.setdefault(action.robot_id, {})
        for resource in action.resources:
            existing = holders.get(resource)
            if existing is not None and existing.action_id != action.action_id:
                raise ResourceConflict(action.action_id, resource, existing.action_id)
        for resource in action.resources:
            holders[resource] = action

    def release(self, action: RobotAction) -> tuple[Resource, ...]:
        """Give back every resource this action holds. Idempotent; returns what was freed."""
        holders = self._running.get(action.robot_id)
        if holders is None:
            return ()
        freed = tuple(
            resource for resource, held in list(holders.items()) if held.action_id == action.action_id
        )
        for resource in freed:
            del holders[resource]
        if not holders:
            del self._running[action.robot_id]
        return freed

    def claimed(self, robot_id: str) -> frozenset[Resource]:
        return frozenset(self._running.get(robot_id, {}))

    # -- dispatch selection --------------------------------------------------------------------

    def next_ready(self, robot_id: str) -> RobotAction | None:
        """The highest-priority pending action whose resources are all free.

        Head-of-line blocking is deliberate only for the resources actually contended: a
        head action still runs while a drive action waits, because they claim nothing in
        common. The queue is scanned in dispatch order, so a lower-priority action can
        only overtake a higher-priority one that is blocked on a different resource.
        """
        for action in self._pending.get(robot_id, ()):
            if self.is_free(action):
                return action
        return None

    def next_blocked(self, robot_id: str) -> RobotAction | None:
        """The head of the queue when it cannot run. What preemption is evaluated against."""
        queue = self._pending.get(robot_id, ())
        return queue[0] if queue and not self.is_free(queue[0]) else None

    def forget(self, robot_id: str) -> tuple[RobotAction, ...]:
        """Drop everything for one robot. Returns what was pending or running."""
        pending = tuple(self._pending.pop(robot_id, []))
        holders = self._running.pop(robot_id, {})
        running = tuple(sorted(set(holders.values()), key=lambda action: action.action_id))
        return pending + running

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._pending.values())

    def __repr__(self) -> str:
        claims = {robot: sorted(res.value for res in holders) for robot, holders in self._running.items()}
        return f"<RobotActionQueue pending={len(self)} claims={claims}>"


def unique(actions: Iterable[RobotAction]) -> tuple[RobotAction, ...]:
    """De-duplicate by action id, preserving order. A drained queue can repeat an action."""
    seen: dict[str, RobotAction] = {}
    for action in actions:
        seen.setdefault(action.action_id, action)
    return tuple(seen.values())


__all__ = ["ResourceConflict", "RobotActionQueue", "unique"]
