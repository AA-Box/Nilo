"""Loading animations from files, and the expression vocabulary they may use.

The library is a directory of YAML (or JSON) documents. ``robot/animation/library/`` holds
the ones that ship; a deployment points :func:`load_library` at its own directory to add
or replace them. Adding an animation is adding a file — no Python, no registration call.

A file may hold one animation (a mapping with a ``name``) or several (a mapping under an
``animations`` key, or a list). Names must be unique across the library: a duplicate is an
error rather than a silent last-one-wins, because two files defining ``greet`` is somebody
copying instead of editing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from robot.animation.model import Animation

logger = logging.getLogger(__name__)

#: Where the animations that ship with the robot live.
BUILTIN_LIBRARY_DIR = Path(__file__).resolve().parent / "library"

#: Where a deployment's own animations live, relative to the server directory. Loaded
#: after the built-ins, and allowed to replace them by name.
DEFAULT_LIBRARY_DIR = Path("data") / "animations"

SUFFIXES = frozenset({".yaml", ".yml", ".json"})

#: The semantic expressions an animation (or a behaviour) may ask the face for. A closed
#: set so a typo is caught on load rather than showing nothing on the robot. Firmware maps
#: each of these onto whatever its display can do — the backend never sends pixels.
EXPRESSIONS: tuple[str, ...] = (
    "neutral",
    "happy",
    "excited",
    "curious",
    "confused",
    "sad",
    "sleepy",
    "surprised",
    "annoyed",
    "scared",
    "focused",
)


class AnimationError(ValueError):
    """A library that will not load: a duplicate name, a bad channel, an unreadable file."""


class AnimationLibrary:
    """Animations by name, plus the lookups a behaviour actually performs."""

    def __init__(self, animations: Iterable[Animation] = ()) -> None:
        self._animations: dict[str, Animation] = {}
        for animation in animations:
            self.add(animation)

    def add(self, animation: Animation, *, replace: bool = False) -> Animation:
        if animation.name in self._animations and not replace:
            raise AnimationError(f"two animations are named {animation.name!r}")
        self._animations[animation.name] = animation
        return animation

    def get(self, name: str) -> Animation | None:
        return self._animations.get(name)

    def require(self, name: str) -> Animation:
        animation = self._animations.get(name)
        if animation is None:
            raise AnimationError(f"no animation named {name!r}; known: {', '.join(self.names())}")
        return animation

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._animations))

    def tagged(self, tag: str) -> tuple[Animation, ...]:
        """Every animation carrying a tag, by name. For "any greeting will do"."""
        return tuple(
            self._animations[name] for name in self.names() if tag in self._animations[name].tags
        )

    def __len__(self) -> int:
        return len(self._animations)

    def __contains__(self, name: object) -> bool:
        return name in self._animations

    def __iter__(self) -> Iterator[Animation]:
        return iter(self._animations[name] for name in self.names())

    def __repr__(self) -> str:
        return f"<AnimationLibrary {len(self._animations)} animations>"


def parse_document(data: Any, *, source: str = "<memory>") -> list[Animation]:
    """Every animation in one parsed document."""
    if data is None:
        return []
    if isinstance(data, list):
        entries: list[Any] = list(data)
    elif isinstance(data, Mapping):
        if "animations" in data:
            block = data["animations"]
            if isinstance(block, Mapping):
                # {name: {...}} — the name is the key rather than a field.
                entries = [{"name": name, **value} for name, value in block.items()]
            else:
                entries = list(block)
        else:
            entries = [data]
    else:
        raise AnimationError(f"{source}: an animation document must be a mapping or a list")
    animations = []
    for entry in entries:
        try:
            animations.append(Animation.model_validate(entry))
        except Exception as exc:
            raise AnimationError(f"{source}: {exc}") from exc
    return animations


def load_file(path: str | Path) -> list[Animation]:
    """Every animation in one file. YAML or JSON, decided by the suffix."""
    location = Path(path)
    text = location.read_text(encoding="utf-8")
    if location.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    return parse_document(data, source=str(location))


def load_directory(directory: str | Path, library: AnimationLibrary | None = None, *, replace: bool = False) -> AnimationLibrary:
    """Load every animation file in a directory, in name order so a run is reproducible."""
    # `library or AnimationLibrary()` would be wrong: an empty library is falsy (it has a
    # __len__), so the caller's would be silently replaced by a fresh one.
    target = AnimationLibrary() if library is None else library
    root = Path(directory)
    if not root.is_dir():
        logger.debug("no animation directory at %s", root)
        return target
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in SUFFIXES:
            continue
        for animation in load_file(path):
            target.add(animation, replace=replace)
    return target


def load_library(extra: str | Path | None = None, *, builtins: bool = True) -> AnimationLibrary:
    """The animations that ship, plus a deployment's own.

    The deployment directory is loaded second and may replace a built-in by name, which is
    how an owner retunes ``excited_greeting`` for a chassis with no lift without forking
    the package.
    """
    library = AnimationLibrary()
    if builtins:
        load_directory(BUILTIN_LIBRARY_DIR, library)
    location = Path(extra) if extra is not None else DEFAULT_LIBRARY_DIR
    if location.is_dir():
        load_directory(location, library, replace=True)
        logger.info("robot animations: %d loaded (%s plus %s)", len(library), BUILTIN_LIBRARY_DIR.name, location)
    return library


__all__ = [
    "BUILTIN_LIBRARY_DIR",
    "DEFAULT_LIBRARY_DIR",
    "EXPRESSIONS",
    "AnimationError",
    "AnimationLibrary",
    "load_directory",
    "load_file",
    "load_library",
    "parse_document",
]
