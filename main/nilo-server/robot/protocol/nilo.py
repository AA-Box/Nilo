"""The Nilo device protocol: routes and the reserved wire vocabulary."""

from robot.protocol.base import ProtocolSpec

NILO = ProtocolSpec(name="nilo", ws_path="/nilo/v1/", ota_path="/nilo/ota/")

# JSON "type" values the session already uses on the wire (core/handle/textHandler/
# and the server -> device messages). A new message type must not reuse one of these.
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
