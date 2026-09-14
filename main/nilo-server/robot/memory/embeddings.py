"""Embeddings, and the fact that they are optional.

A robot must work with embeddings **disabled**. That is the requirement, and it shapes
everything here: the default provider is :class:`NullEmbeddingProvider`, which produces
nothing, and every retrieval path scores fine without a single vector (``retrieval.py``
falls back to lexical overlap, recency and importance).

When embeddings are enabled, they add one signal to ranking. They do not replace the
others, and they are never the only way to find something — "what did I do yesterday" is a
timestamp query, and no amount of cosine similarity improves it.

:class:`HashingEmbeddingProvider` is a real, local, dependency-free provider: a hashed
bag-of-words projection. It is not a language model and does not pretend to be — it
captures word overlap, which is exactly the signal it is used for — but it is deterministic,
costs nothing, needs no download, and makes the embeddings-enabled path testable in CI.
A deployment with a real model implements the same four-line protocol.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Dimensions of the built-in hashing provider. Small: it is a word-overlap signal, not a
#: semantic model, and a 1024-dimension vector of mostly zeros helps nobody.
DEFAULT_DIMENSIONS = 64

_WORD = re.compile(r"[a-z0-9']+")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into a vector, or says it does not do that."""

    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool:
        """Whether this provider actually produces vectors. ``False`` is a valid answer."""

    async def embed(self, text: str) -> list[float]:
        """The vector for one piece of text. Empty when the provider is disabled."""


class NullEmbeddingProvider:
    """Produces nothing. The default, and a complete implementation.

    A memory system whose retrieval degrades to "nothing works" without embeddings has the
    dependency the design was trying to avoid. With this provider installed, every test in
    ``tests/robot/test_memory.py`` still passes.
    """

    name = "null"
    enabled = False

    async def embed(self, text: str) -> list[float]:
        return []


class HashingEmbeddingProvider:
    """A deterministic local embedding: hashed word counts, L2-normalized.

    No model, no download, no network, and the same vector on every machine — which is
    what makes "semantic retrieval is on" a thing CI can actually check.
    """

    name = "hashing"
    enabled = True

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 8:
            raise ValueError("an embedding needs at least eight dimensions to be worth anything")
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)

    def embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            # The sign bit spreads collisions in both directions, so two unrelated words
            # landing in one bucket cancel rather than reinforce.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity mapped into 0..1. Mismatched or empty vectors score zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1) / 2))


__all__ = [
    "DEFAULT_DIMENSIONS",
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "NullEmbeddingProvider",
    "cosine",
]
