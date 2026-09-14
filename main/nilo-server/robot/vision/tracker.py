"""Turning per-frame detections into things with identities that survive a frame.

A detector answers "what is in this image". A tracker answers "is that the same person as
last time", which is the question every behaviour actually asks — a greeting that fires
once per frame is not a greeting.

The implementation is deliberately the simple one: associate by box overlap, fall back to
centre distance, keep an id alive for a few frames after it disappears. That is enough for
a robot looking at a room from one camera, it has no parameters nobody can explain, and it
is fully deterministic — the same detections in the same order produce the same tracks, on
every machine, which is what makes the tests exact rather than statistical.

    # ponytail: greedy IoU association, O(tracks x detections). A proper assignment
    # (Hungarian) matters when a frame holds a dozen overlapping people; a robot looking
    # at its owner's living room does not.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from robot.vision.types import BoundingBox, Detection, DetectionKind, FaceIdentity

#: Minimum overlap for two boxes to be the same thing.
DEFAULT_IOU_THRESHOLD = 0.25

#: Maximum normalized centre distance for a fallback match, when boxes do not overlap at
#: all. A person walking quickly can move a third of the frame between two snapshots.
DEFAULT_DISTANCE_THRESHOLD = 0.35

#: How many consecutive frames a track may go unseen before it is dropped. Snapshots are
#: slow (a capture is a round trip to the device), so three frames is a real timeout, not
#: a video tracker's flicker window.
DEFAULT_MAX_MISSES = 3

#: How many frames a track must be seen in before anything downstream is told about it.
#: One frame of a coat on a chair is not a person.
DEFAULT_MIN_HITS = 1


@dataclass
class Track:
    """One thing, followed across frames."""

    id: str
    kind: DetectionKind
    box: BoundingBox
    confidence: float = 1.0
    label: str = ""
    identity: FaceIdentity | None = None
    hits: int = 1
    misses: int = 0
    first_frame: int = 0
    last_frame: int = 0
    #: Where the box centre has been, newest last. Bounded; used for direction.
    history: list[tuple[float, float]] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.hits >= DEFAULT_MIN_HITS

    @property
    def centre(self) -> tuple[float, float]:
        return self.box.centre

    def velocity(self) -> tuple[float, float]:
        """Normalized movement per frame, from the last two sightings. Zero for a new track."""
        if len(self.history) < 2:
            return (0.0, 0.0)
        (px, py), (cx, cy) = self.history[-2], self.history[-1]
        return (cx - px, cy - py)

    def direction(self) -> str:
        """``left``, ``right`` or ``still``. What a scenario assertion reads."""
        dx, _ = self.velocity()
        if abs(dx) < 0.02:
            return "still"
        return "right" if dx > 0 else "left"

    def approaching(self) -> bool:
        """Whether the box is growing, which for one camera is what "coming closer" means."""
        return len(self.history) >= 2 and self.box.area > self._previous_area

    def update(self, detection: Detection, frame_index: int) -> None:
        self._previous_area = self.box.area
        self.box = detection.box
        self.confidence = detection.confidence
        self.label = detection.label or self.label
        if detection.identity is not None:
            self.identity = detection.identity
        self.hits += 1
        self.misses = 0
        self.last_frame = frame_index
        self.history.append(detection.box.centre)
        if len(self.history) > 16:  # bounded: a track that lives for an hour is not a leak
            del self.history[0]

    _previous_area: float = 0.0


@runtime_checkable
class Tracker(Protocol):
    """Associates detections with ids that outlive a frame."""

    def update(self, detections: Sequence[Detection]) -> tuple[tuple[Track, ...], tuple[Track, ...]]:
        """Fold one frame in. Returns ``(active_tracks, lost_tracks)``."""

    def tracks(self) -> tuple[Track, ...]: ...

    def reset(self) -> None: ...


class CentroidTracker:
    """Greedy IoU association with a centre-distance fallback and a miss timeout.

    Ids are ``<kind>-<n>``, allocated in order, so a test can name the second person it
    spawned. The counter is per tracker instance, not global: two robots never share an
    id space.
    """

    def __init__(
        self,
        *,
        iou_threshold: float = DEFAULT_IOU_THRESHOLD,
        distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
        max_misses: int = DEFAULT_MAX_MISSES,
        min_hits: int = DEFAULT_MIN_HITS,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.distance_threshold = distance_threshold
        self.max_misses = max_misses
        self.min_hits = min_hits
        self._tracks: dict[str, Track] = {}
        self._counters: dict[DetectionKind, itertools.count[int]] = {}
        self._frame_index = 0

    def tracks(self) -> tuple[Track, ...]:
        """Every live track, in id order so iteration is never dict-order dependent."""
        return tuple(self._tracks[key] for key in sorted(self._tracks))

    def get(self, track_id: str) -> Track | None:
        return self._tracks.get(track_id)

    def reset(self) -> None:
        self._tracks.clear()
        self._counters.clear()
        self._frame_index = 0

    def update(self, detections: Sequence[Detection]) -> tuple[tuple[Track, ...], tuple[Track, ...]]:
        """Fold one frame in and return ``(active, lost)``.

        Matching is greedy over the best score first, so the assignment does not depend on
        the order the detector happened to return boxes in — which is the difference
        between a deterministic tracker and one that renumbers people at random.
        """
        self._frame_index += 1
        pairs = self._score_pairs(detections)
        matched_tracks: set[str] = set()
        matched_detections: set[int] = set()
        for _score, track_id, index in pairs:
            if track_id in matched_tracks or index in matched_detections:
                continue
            matched_tracks.add(track_id)
            matched_detections.add(index)
            self._tracks[track_id].update(detections[index], self._frame_index)

        for index, detection in enumerate(detections):
            if index not in matched_detections:
                track = self._new_track(detection)
                self._tracks[track.id] = track

        lost: list[Track] = []
        for track_id, track in list(self._tracks.items()):
            if track_id in matched_tracks or track.last_frame == self._frame_index:
                continue
            track.misses += 1
            if track.misses > self.max_misses:
                lost.append(self._tracks.pop(track_id))
        return (tuple(t for t in self.tracks() if t.hits >= self.min_hits), tuple(lost))

    def _score_pairs(self, detections: Sequence[Detection]) -> list[tuple[float, str, int]]:
        """Every plausible (track, detection) pairing, best first.

        Sorted by score then by ids, so two pairings with identical scores resolve the
        same way every run.
        """
        pairs: list[tuple[float, str, int]] = []
        for track in self.tracks():
            for index, detection in enumerate(detections):
                if detection.kind is not track.kind:
                    continue
                iou = track.box.iou(detection.box)
                if iou >= self.iou_threshold:
                    pairs.append((iou, track.id, index))
                    continue
                distance = track.box.distance_to(detection.box)
                if distance <= self.distance_threshold:
                    # Below the IoU threshold a distance match is worth less than any
                    # overlap match, so it never outranks one.
                    pairs.append((self.iou_threshold * (1 - distance), track.id, index))
        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        return pairs

    def _new_track(self, detection: Detection) -> Track:
        counter = self._counters.setdefault(detection.kind, itertools.count(1))
        track = Track(
            id=f"{detection.kind.value}-{next(counter)}",
            kind=detection.kind,
            box=detection.box,
            confidence=detection.confidence,
            label=detection.label,
            identity=detection.identity,
            first_frame=self._frame_index,
            last_frame=self._frame_index,
            history=[detection.box.centre],
        )
        return track

    def __len__(self) -> int:
        return len(self._tracks)

    def __repr__(self) -> str:
        return f"<CentroidTracker {len(self._tracks)} tracks frame={self._frame_index}>"


__all__ = [
    "DEFAULT_DISTANCE_THRESHOLD",
    "DEFAULT_IOU_THRESHOLD",
    "DEFAULT_MAX_MISSES",
    "DEFAULT_MIN_HITS",
    "CentroidTracker",
    "Track",
    "Tracker",
]
