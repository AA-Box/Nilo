"""Protocol registry: the Nilo routes, enable/disable from config, path matching."""
import pytest

from robot.protocol import ALL_PROTOCOLS, NILO, RESERVED_MESSAGE_TYPES, ProtocolSpec, registry_from_config
from robot.protocol.base import ProtocolRegistry


def test_nilo_routes():
    assert NILO.ws_path == "/nilo/v1/"
    assert NILO.ota_path == "/nilo/ota/"
    assert NILO.ota_download_path == "/nilo/ota/download/{filename}"


def test_nilo_is_the_only_protocol():
    assert [s.name for s in ALL_PROTOCOLS] == ["nilo"]


def test_paths_must_be_slashed():
    with pytest.raises(ValueError):
        ProtocolSpec(name="x", ws_path="nilo/v1", ota_path="/x/")


def test_default_config_enables_nilo():
    reg = registry_from_config({})
    assert [s.name for s in reg.enabled()] == ["nilo"]
    assert reg.default().name == "nilo"
    assert reg.ws_url("10.0.0.5", 8000) == "ws://10.0.0.5:8000/nilo/v1/"


def test_disabled_protocol_has_no_default_and_accepts_nothing():
    reg = registry_from_config({"protocols": {"nilo": {"enabled": False}}})
    assert reg.enabled() == []
    assert not reg.ws_accepts("/nilo/v1/")
    with pytest.raises(RuntimeError):
        reg.default()


@pytest.mark.parametrize("path", ["/nilo/v1/", "/nilo/v1", "/nilo/v1/?device-id=aa:bb", "/nilo/v1?x=1"])
def test_ws_path_matching_ignores_query_and_trailing_slash(path):
    reg = registry_from_config({})
    assert reg.match_ws_path(path) is not None
    assert reg.match_ws_path(path).name == "nilo"


def test_unknown_paths_are_rejected_by_default():
    """Retired routes must not keep working: strict is on unless explicitly disabled."""
    reg = registry_from_config({})
    assert reg.strict is True
    assert not reg.ws_accepts("/xiaozhi/v1/")
    assert not reg.ws_accepts("/anything/")


def test_strict_can_be_turned_off():
    reg = registry_from_config({"protocols": {"strict": False}})
    assert reg.ws_accepts("/anything/")
    assert reg.ws_accepts("/nilo/v1/")


def test_bad_protocols_block_rejected():
    with pytest.raises(ValueError):
        ProtocolRegistry.from_config({"protocols": "yes"}, ALL_PROTOCOLS)


def test_reserved_message_types_cover_the_wire_vocabulary():
    assert {"hello", "listen", "mcp", "abort", "tts", "stt"} <= RESERVED_MESSAGE_TYPES
