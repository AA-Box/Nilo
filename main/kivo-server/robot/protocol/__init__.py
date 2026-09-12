"""Device-facing protocol registry.

Kivo currently speaks one wire protocol *family*: a WebSocket session carrying
JSON control messages and binary Opus audio, bootstrapped by an HTTP OTA
endpoint. Two named protocols expose that family on different routes:

* ``kivo`` — the Kivo-native routes (``/kivo/v1/``, ``/kivo/ota/``).
* ``legacy_xiaozhi`` — the routes hard-coded in existing ESP32 firmware
  (``/xiaozhi/v1/``, ``/xiaozhi/ota/``), kept for compatibility.

The rest of the application asks :class:`ProtocolRegistry` which routes exist
instead of spelling out a path, so legacy terminology stays inside
``legacy_xiaozhi.py``.
"""

from collections.abc import Mapping
from typing import Any

from robot.protocol.base import ProtocolRegistry, ProtocolSpec
from robot.protocol.kivo import KIVO
from robot.protocol.legacy_xiaozhi import LEGACY_XIAOZHI

ALL_PROTOCOLS: tuple[ProtocolSpec, ...] = (KIVO, LEGACY_XIAOZHI)


def registry_from_config(config: Mapping[str, Any]) -> ProtocolRegistry:
    """Build the registry from the ``protocols:`` block of the server config."""
    return ProtocolRegistry.from_config(config, ALL_PROTOCOLS)


__all__ = [
    "ALL_PROTOCOLS",
    "KIVO",
    "LEGACY_XIAOZHI",
    "ProtocolRegistry",
    "ProtocolSpec",
    "registry_from_config",
]
