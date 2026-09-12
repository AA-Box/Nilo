"""Compatibility routes for existing Xiaozhi-family ESP32 firmware.

The firmware bakes the OTA URL in at compile time (``/xiaozhi/ota/``) and then
connects to whatever WebSocket URL the OTA response returns. Keeping this
protocol enabled lets unmodified devices bootstrap against Kivo. Disable it
with ``protocols.legacy_xiaozhi.enabled: false`` once every device has been
re-flashed to use the Kivo routes.

This module is the only place in the application that may spell out legacy
route names.
"""

from robot.protocol.base import ProtocolSpec

LEGACY_XIAOZHI = ProtocolSpec(
    name="legacy_xiaozhi",
    ws_path="/xiaozhi/v1/",
    ota_path="/xiaozhi/ota/",
)

# JSON "type" values already used on the wire by legacy clients and servers.
# New Kivo message types must not reuse these names.
RESERVED_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "listen",
        "stt",
        "tts",
        "llm",
        "abort",
        "iot",
        "mcp",
        "server",
        "ping",
        "notify",
        "alert",
        "custom",
        "system",
        "goodbye",
    }
)
