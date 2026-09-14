"""Persisting personality — and deliberately not persisting every twitch of the state.

Two different things with two different lifetimes:

* **Traits** are stable and worth keeping. They are written when they change and read at
  startup, so a robot is the same robot after a restart.
* **The control variables** move constantly. Writing them on every change would be a file
  write per tick, and restoring them exactly would resurrect a five-hour-old mood. They
  are snapshotted coarsely (two decimal places), at most once every
  :attr:`PersonalityStore.min_interval_s`, and on a clean shutdown.

One JSON file per robot, written atomically (write to a temporary file in the same
directory, then ``os.replace``), so a crash mid-write leaves the previous file intact
rather than a truncated one. This is **not** the pattern in
``core/providers/memory/mem_local_short/``, which read-modify-writes one shared YAML file
for every robot in the process.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robot.personality.emotion import EmotionalState
from robot.personality.traits import PersonalityTraits
from robot.state.models import normalize_robot_id, utcnow

logger = logging.getLogger(__name__)

#: Where personalities live, relative to the server directory.
DEFAULT_STORE_DIR = Path("data") / "robot_personality"

#: How coarse the persisted control variables are. Two places is a mood, not a twitch.
SNAPSHOT_PLACES = 2


class PersonalityRecord(BaseModel):
    """What is on disk for one robot."""

    model_config = ConfigDict(extra="forbid")

    robot_id: str
    traits: PersonalityTraits = Field(default_factory=PersonalityTraits)
    #: A coarse snapshot of the control variables. Advisory: a robot that starts without
    #: one is not broken, it is just in a neutral mood.
    state: EmotionalState | None = None
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())
    version: int = 1


class PersonalityStore:
    """One JSON file per robot, under a directory this owns.

    Constructed per runtime. There is no module-level store, so two tests never share a
    directory and two processes never fight over one file.
    """

    def __init__(self, directory: str | Path | None = None, *, min_interval_s: float = 60.0) -> None:
        self.directory = Path(directory) if directory is not None else DEFAULT_STORE_DIR
        self.min_interval_s = min_interval_s
        self._last_write: dict[str, float] = {}

    def path_for(self, robot_id: str) -> Path:
        return self.directory / f"{normalize_robot_id(robot_id)}.json"

    def load(self, robot_id: str) -> PersonalityRecord | None:
        """Read one robot's record, or ``None`` if it has never been written.

        A file that exists but will not parse is logged and treated as absent: a corrupt
        personality file must not stop a robot from starting, and the traits it holds are
        a preference rather than a safety input.
        """
        path = self.path_for(robot_id)
        if not path.exists():
            return None
        try:
            return PersonalityRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("robot %s: personality file %s is unreadable (%s); starting fresh", robot_id, path, exc)
            return None

    def save(
        self,
        robot_id: str,
        traits: PersonalityTraits,
        state: EmotionalState | None = None,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        """Write the record. Returns whether it actually wrote.

        Throttled: a call inside ``min_interval_s`` of the last write is skipped unless
        ``force``. Trait changes and shutdown pass ``force``; the periodic mood snapshot
        does not.
        """
        if not force and now is not None:
            last = self._last_write.get(robot_id)
            if last is not None and now - last < self.min_interval_s:
                return False
        record = PersonalityRecord(
            robot_id=robot_id,
            traits=traits,
            state=state.rounded(SNAPSHOT_PLACES) if state is not None else None,
        )
        self._write(self.path_for(robot_id), record.model_dump_json(indent=2))
        if now is not None:
            self._last_write[robot_id] = now
        return True

    def delete(self, robot_id: str) -> bool:
        """Forget a robot's personality entirely. Returns whether anything was removed."""
        path = self.path_for(robot_id)
        if not path.exists():
            return False
        path.unlink()
        self._last_write.pop(robot_id, None)
        return True

    def list_robots(self) -> tuple[str, ...]:
        if not self.directory.is_dir():
            return ()
        return tuple(sorted(path.stem for path in self.directory.glob("*.json")))

    @staticmethod
    def _write(path: Path, payload: str) -> None:
        """Atomic replace, so a crash mid-write cannot truncate the previous record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def __repr__(self) -> str:
        return f"<PersonalityStore {self.directory} robots={len(self.list_robots())}>"


def personality_json(record: PersonalityRecord) -> dict[str, Any]:
    """The record as plain data, for an API response."""
    payload: dict[str, Any] = json.loads(record.model_dump_json())
    return payload


__all__ = [
    "DEFAULT_STORE_DIR",
    "SNAPSHOT_PLACES",
    "PersonalityRecord",
    "PersonalityStore",
    "personality_json",
]
