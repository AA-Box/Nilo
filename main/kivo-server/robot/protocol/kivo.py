"""Kivo-native routes.

Wire format is currently identical to the legacy protocol (see
``docs/protocol.md``); the separate routes exist so Kivo can evolve its own
handshake without breaking devices that were flashed against the legacy paths.
"""

from robot.protocol.base import ProtocolSpec

KIVO = ProtocolSpec(name="kivo", ws_path="/kivo/v1/", ota_path="/kivo/ota/")
