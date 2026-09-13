"""The action registry: every action the executor has seen, live and retired.

Three jobs:

* **Query state.** ``registry.record(action_id)`` answers "what happened to that action"
  for a caller that did not keep the object — a management API, an LLM follow-up turn, an
  incident log.
* **Correlate device completions.** The device answers a motion command with *its own*
  action id and reports completion against that id, not ours. The registry holds the
  mapping, which is what makes a late or duplicated ``notifications/motion_completed``
  resolvable to the action it belongs to (or to nothing, which is also an answer).
* **Bound the history.** Retired actions go into a fixed-length deque. The inherited
  server has an unbounded dialogue list that grows for as long as a session lives
  (docs/robot-architecture.md R12); a robot that is up for a week must not accumulate one
  dict per motion command.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator

from robot.actions.model import RobotAction
from robot.state.actions import ActionRecord, ActionStatus, ActionType

#: How many terminal actions to keep per process before the oldest is forgotten.
DEFAULT_HISTORY_LIMIT = 512


class RobotActionRegistry:
    """Live actions by id, retired actions in a bounded history, device ids mapped to both."""

    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be at least 1")
        self._live: dict[str, RobotAction] = {}
        self._history: deque[ActionRecord] = deque(maxlen=history_limit)
        self._device_ids: dict[tuple[str, str], str] = {}

    # -- registration -----------------------------------------------------------------------

    def add(self, action: RobotAction) -> RobotAction:
        """Track a newly created action. Re-adding the same id is a no-op."""
        self._live.setdefault(action.action_id, action)
        return action

    def bind_device_action(self, action: RobotAction, device_action_id: str) -> None:
        """Record the id the device gave this action, so its completion can be found.

        A device that reuses an id for a later command simply rebinds it: the newest
        claimant wins, which matches the firmware behaviour of handing out a fresh id per
        accepted motion.
        """
        action.device_action_id = device_action_id
        self._device_ids[(action.robot_id, device_action_id)] = action.action_id

    def retire(self, action: RobotAction) -> ActionRecord:
        """Move a terminal action out of the live index and into the bounded history."""
        record = action.record()
        self._live.pop(action.action_id, None)
        if action.device_action_id is not None:
            self._device_ids.pop((action.robot_id, action.device_action_id), None)
        self._history.append(record)
        return record

    def forget_robot(self, robot_id: str) -> tuple[RobotAction, ...]:
        """Drop the live actions of one robot. History is kept: it is the audit trail."""
        dropped = tuple(action for action in self._live.values() if action.robot_id == robot_id)
        for action in dropped:
            self._live.pop(action.action_id, None)
            if action.device_action_id is not None:
                self._device_ids.pop((robot_id, action.device_action_id), None)
        return dropped

    # -- queries -----------------------------------------------------------------------------

    def get(self, action_id: str) -> RobotAction | None:
        """The live action object, or ``None`` if it is unknown or already retired."""
        return self._live.get(action_id)

    def by_device_action(self, robot_id: str, device_action_id: str) -> RobotAction | None:
        """The live action a device completion belongs to, or ``None``.

        ``None`` is the expected answer for a duplicate completion: the first one retired
        the action and removed the mapping, so the second resolves to nothing and is
        dropped rather than double-settling an action.
        """
        action_id = self._device_ids.get((robot_id, device_action_id))
        return self._live.get(action_id) if action_id is not None else None

    def record(self, action_id: str) -> ActionRecord | None:
        """The snapshot of one action, live or retired. The "query state" entry point."""
        action = self._live.get(action_id)
        if action is not None:
            return action.record()
        for record in reversed(self._history):
            if record.action_id == action_id:
                return record
        return None

    def live(
        self,
        robot_id: str | None = None,
        *,
        status: ActionStatus | None = None,
        action_type: ActionType | None = None,
    ) -> tuple[RobotAction, ...]:
        """Live action objects matching the filters, oldest first."""
        actions = [
            action
            for action in self._live.values()
            if (robot_id is None or action.robot_id == robot_id)
            and (status is None or action.status is status)
            and (action_type is None or action.action_type is action_type)
        ]
        actions.sort(key=lambda action: action.created_at)
        return tuple(actions)

    def records(
        self,
        robot_id: str | None = None,
        *,
        status: ActionStatus | None = None,
        include_history: bool = True,
        limit: int | None = None,
    ) -> tuple[ActionRecord, ...]:
        """Snapshots, newest first. The read model for an API or a log dump."""
        found: list[ActionRecord] = [action.record() for action in self._live.values()]
        if include_history:
            found.extend(self._history)
        matching = [
            record
            for record in found
            if (robot_id is None or record.robot_id == robot_id)
            and (status is None or record.status is status)
        ]
        matching.sort(key=lambda record: record.created_at, reverse=True)
        return tuple(matching if limit is None else matching[:limit])

    @property
    def history(self) -> tuple[ActionRecord, ...]:
        return tuple(self._history)

    def __len__(self) -> int:
        return len(self._live)

    def __iter__(self) -> Iterator[RobotAction]:
        return iter(tuple(self._live.values()))

    def __contains__(self, action_id: object) -> bool:
        return isinstance(action_id, str) and action_id in self._live

    def __repr__(self) -> str:
        return f"<RobotActionRegistry live={len(self._live)} history={len(self._history)}>"


def records_of(actions: Iterable[RobotAction]) -> tuple[ActionRecord, ...]:
    return tuple(action.record() for action in actions)


__all__ = ["DEFAULT_HISTORY_LIMIT", "RobotActionRegistry", "records_of"]
