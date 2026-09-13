"""Device-facing protocol registry.

Nilo speaks one wire protocol: a WebSocket session carrying JSON control
messages and binary Opus audio, bootstrapped by an HTTP OTA endpoint. The
``nilo`` protocol exposes it on ``/nilo/v1/`` and ``/nilo/ota/``.

The rest of the application asks :class:`ProtocolRegistry` which routes exist
instead of spelling out a path, so a future revision of the protocol can be
added here (as a second :class:`ProtocolSpec`) without touching the server.
"""

from collections.abc import Mapping
from typing import Any

from robot.protocol.base import ProtocolRegistry, ProtocolSpec
from robot.protocol.nilo import NILO, RESERVED_MESSAGE_TYPES

ALL_PROTOCOLS: tuple[ProtocolSpec, ...] = (NILO,)


def registry_from_config(config: Mapping[str, Any]) -> ProtocolRegistry:
    """Build the registry from the ``protocols:`` block of the server config."""
    return ProtocolRegistry.from_config(config, ALL_PROTOCOLS)


__all__ = [
    "ALL_PROTOCOLS",
    "NILO",
    "RESERVED_MESSAGE_TYPES",
    "ProtocolRegistry",
    "ProtocolSpec",
    "registry_from_config",
]
