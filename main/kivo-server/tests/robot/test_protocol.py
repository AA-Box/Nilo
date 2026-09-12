"""Protocol registry: routes per protocol, enable/disable from config, path matching."""
import pytest

from robot.protocol import ALL_PROTOCOLS, KIVO, LEGACY_XIAOZHI, ProtocolSpec, registry_from_config
from robot.protocol.base import ProtocolRegistry
from robot.protocol.legacy_xiaozhi import RESERVED_MESSAGE_TYPES


def test_kivo_and_legacy_routes_are_distinct():
    assert KIVO.ws_path == "/kivo/v1/"
    assert KIVO.ota_path == "/kivo/ota/"
    assert LEGACY_XIAOZHI.ws_path == "/xiaozhi/v1/"
    assert LEGACY_XIAOZHI.ota_path == "/xiaozhi/ota/"
    assert KIVO.ota_download_path == "/kivo/ota/download/{filename}"


def test_paths_must_be_slashed():
    with pytest.raises(ValueError):
        ProtocolSpec(name="x", ws_path="kivo/v1", ota_path="/x/")


def test_default_config_enables_both_and_prefers_kivo():
    reg = registry_from_config({})
    assert [s.name for s in reg.enabled()] == ["kivo", "legacy_xiaozhi"]
    assert reg.default() is not None and reg.default().name == "kivo"
    assert reg.ws_url("10.0.0.5", 8000) == "ws://10.0.0.5:8000/kivo/v1/"


def test_legacy_can_be_disabled():
    reg = registry_from_config({"protocols": {"legacy_xiaozhi": {"enabled": False}}})
    assert [s.name for s in reg.enabled()] == ["kivo"]
    assert reg.ws_accepts("/kivo/v1/")
    assert not reg.ws_accepts("/xiaozhi/v1/")


def test_kivo_disabled_falls_back_to_legacy_default():
    reg = registry_from_config({"protocols": {"kivo": {"enabled": False}}})
    assert reg.default().name == "legacy_xiaozhi"
    assert reg.ws_url("h", 1) == "ws://h:1/xiaozhi/v1/"


def test_all_disabled_has_no_default():
    reg = registry_from_config({"protocols": {"kivo": {"enabled": False}, "legacy_xiaozhi": {"enabled": False}}})
    with pytest.raises(RuntimeError):
        reg.default()


@pytest.mark.parametrize("path", ["/kivo/v1/", "/kivo/v1", "/kivo/v1/?device-id=aa:bb", "/kivo/v1?x=1"])
def test_ws_path_matching_ignores_query_and_trailing_slash(path):
    reg = registry_from_config({})
    assert reg.match_ws_path(path) is not None and reg.match_ws_path(path).name == "kivo"


def test_unknown_path_permissive_by_default_strict_rejects():
    assert registry_from_config({}).ws_accepts("/anything/")
    assert not registry_from_config({"protocols": {"strict": True}}).ws_accepts("/anything/")


def test_bad_protocols_block_rejected():
    with pytest.raises(ValueError):
        ProtocolRegistry.from_config({"protocols": "yes"}, ALL_PROTOCOLS)


def test_reserved_message_types_cover_the_wire_vocabulary():
    assert {"hello", "listen", "mcp", "abort", "tts", "stt"} <= RESERVED_MESSAGE_TYPES
