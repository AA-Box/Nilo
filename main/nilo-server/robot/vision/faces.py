"""Face identity: who a face belongs to, and where that claim is kept.

Four fields, and they are the whole contract (docs/robot-vision.md):

``person_id``      stable id for a person, allocated here and used everywhere else
``display_name``   what to call them out loud. Optional: an unknown person is still a person
``embedding_ref``  a **reference** to a vector, never the vector itself
``confidence``     how sure the recognizer is, 0..1

The distinction between an embedding and a reference is deliberate. A face embedding is
biometric data. Keeping it in one registry with one lifetime and passing an id around
means there is exactly one place to delete it from when somebody asks — which Phase 7's
``delete person`` needs to be able to promise.

The registry here is in-memory and matches with cosine similarity over plain Python lists.
It needs no model and no numpy, which makes the whole pipeline testable; a deployment that
wants real face recognition supplies its own :class:`~robot.vision.providers.FaceRecognizer`
and keeps this interface.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from uuid import uuid4

from robot.state.models import utcnow
from robot.vision.types import Detection, FaceIdentity, Frame

logger = logging.getLogger(__name__)

#: Cosine similarity above which two embeddings are considered the same person. High,
#: because the cost of greeting a stranger by the wrong name is worse than the cost of
#: treating a familiar face as new.
DEFAULT_MATCH_THRESHOLD = 0.82


class FaceRecord:
    """One known person, and the vectors seen for them."""

    __slots__ = ("display_name", "embeddings", "first_seen", "last_seen", "person_id", "sightings")

    def __init__(self, person_id: str, display_name: str = "") -> None:
        self.person_id = person_id
        self.display_name = display_name
        self.embeddings: list[tuple[str, list[float]]] = []
        self.first_seen = utcnow()
        self.last_seen = self.first_seen
        self.sightings = 0

    def add(self, embedding: Sequence[float], *, ref: str | None = None) -> str:
        reference = ref or uuid4().hex[:12]
        self.embeddings.append((reference, [float(value) for value in embedding]))
        return reference

    def best_match(self, embedding: Sequence[float]) -> tuple[str, float]:
        """The closest stored vector and its similarity. ``("", 0.0)`` when there are none."""
        best_ref, best_score = "", 0.0
        for ref, stored in self.embeddings:
            score = cosine_similarity(stored, embedding)
            if score > best_score:
                best_ref, best_score = ref, score
        return best_ref, best_score

    def seen(self) -> None:
        self.sightings += 1
        self.last_seen = utcnow()


class FaceRegistry:
    """Known people, by id. The one place a face embedding lives.

    Constructed per runtime. Not a module singleton: two robots in one process may well
    know different people, and a test must never inherit another test's faces.
    """

    def __init__(self, *, match_threshold: float = DEFAULT_MATCH_THRESHOLD) -> None:
        self.match_threshold = match_threshold
        self._people: dict[str, FaceRecord] = {}

    def __len__(self) -> int:
        return len(self._people)

    def __contains__(self, person_id: object) -> bool:
        return person_id in self._people

    def people(self) -> tuple[FaceRecord, ...]:
        return tuple(self._people[key] for key in sorted(self._people))

    def get(self, person_id: str) -> FaceRecord | None:
        return self._people.get(person_id)

    def enroll(
        self,
        embedding: Sequence[float],
        *,
        person_id: str | None = None,
        display_name: str = "",
    ) -> FaceIdentity:
        """Remember a face. Returns the identity to attach to detections of it."""
        identifier = person_id or f"person-{len(self._people) + 1}"
        record = self._people.get(identifier)
        if record is None:
            record = FaceRecord(identifier, display_name)
            self._people[identifier] = record
        elif display_name:
            record.display_name = display_name
        ref = record.add(embedding)
        return FaceIdentity(
            person_id=identifier, display_name=record.display_name, embedding_ref=ref, confidence=1.0
        )

    def identify(self, embedding: Sequence[float]) -> FaceIdentity | None:
        """The best match above the threshold, or ``None`` for somebody new."""
        best: tuple[float, str, str] = (0.0, "", "")
        for record in self.people():
            ref, score = record.best_match(embedding)
            if score > best[0]:
                best = (score, record.person_id, ref)
        score, person_id, ref = best
        if score < self.match_threshold or not person_id:
            return None
        record = self._people[person_id]
        record.seen()
        return FaceIdentity(
            person_id=person_id,
            display_name=record.display_name,
            embedding_ref=ref,
            confidence=round(score, 4),
        )

    def rename(self, person_id: str, display_name: str) -> bool:
        record = self._people.get(person_id)
        if record is None:
            return False
        record.display_name = display_name
        return True

    def forget(self, person_id: str) -> bool:
        """Delete a person and every embedding stored for them. The privacy operation."""
        return self._people.pop(person_id, None) is not None

    def clear(self) -> None:
        self._people.clear()

    def __repr__(self) -> str:
        return f"<FaceRegistry {len(self._people)} people>"


class EmbeddingFaceRecognizer:
    """Matches a detection's embedding against the registry.

    Reads the vector from ``detection.attributes["embedding"]`` — which is where a real
    face model would put it, and where the simulator and the tests do. With no vector
    there is nothing to match, and the answer is an honest ``None`` rather than a guess.

    ``enroll_unknown`` decides what happens to a face nobody recognizes: off (the default)
    reports an unknown person, on adds them to the registry so the robot learns faces it
    keeps seeing. That is a deployment's decision about storing biometric data, so it is a
    flag rather than a behaviour.
    """

    name = "embedding"

    def __init__(self, registry: FaceRegistry | None = None, *, enroll_unknown: bool = False) -> None:
        self.registry = registry if registry is not None else FaceRegistry()
        self.enroll_unknown = enroll_unknown

    async def recognize(self, frame: Frame, detection: Detection) -> FaceIdentity | None:
        embedding = detection.attributes.get("embedding")
        if not isinstance(embedding, (list, tuple)) or not embedding:
            return None
        identity = self.registry.identify([float(value) for value in embedding])
        if identity is not None:
            return identity
        if not self.enroll_unknown:
            return None
        learned = self.registry.enroll(
            [float(value) for value in embedding], display_name=detection.label
        )
        logger.info("vision: enrolled a new face as %s", learned.person_id)
        return learned


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in 0..1. Mismatched lengths and zero vectors score zero."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


__all__ = [
    "DEFAULT_MATCH_THRESHOLD",
    "EmbeddingFaceRecognizer",
    "FaceRecord",
    "FaceRegistry",
    "cosine_similarity",
]
