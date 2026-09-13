"""HTTP route table follows the protocols config; the retired routes must not come back."""
import asyncio

import pytest
pytest.importorskip("aiohttp")  # full runtime dependency; skipped on the dev-only slice
pytest.importorskip("opuslib_next")  # full runtime dependency; skipped on the dev-only slice
pytest.importorskip("numpy")  # full runtime dependency; skipped on the dev-only slice
pytest.importorskip("pydub")  # full runtime dependency; skipped on the dev-only slice

from config.config_loader import load_config  # noqa: E402
from core.http_server import SimpleHttpServer  # noqa: E402


@pytest.fixture
def base_config():
    from core.utils.cache.manager import cache_manager, CacheType

    cache_manager.delete(CacheType.CONFIG, "main_config")
    cfg = asyncio.run(load_config())
    cfg["server"]["auth_key"] = "test-key"
    return cfg


def _paths(app):
    return sorted({r.canonical for r in app.router.resources()})


def test_nilo_ota_routes_are_registered(base_config):
    paths = _paths(SimpleHttpServer(base_config)._build_app())
    assert "/nilo/ota/" in paths and "/nilo/ota/download/{filename}" in paths
    assert "/mcp/vision/explain" in paths


def test_no_retired_routes(base_config):
    """The legacy routes were removed; nothing may re-register them."""
    paths = _paths(SimpleHttpServer(base_config)._build_app())
    assert not any(p.startswith("/xiaozhi/") for p in paths)


def test_disabled_protocol_drops_its_ota_routes(base_config):
    base_config["protocols"] = {"nilo": {"enabled": False}}
    paths = _paths(SimpleHttpServer(base_config)._build_app())
    assert paths == ["/mcp/vision/explain"]


def test_remote_config_mode_has_no_ota_routes(base_config):
    base_config["read_config_from_api"] = True
    paths = _paths(SimpleHttpServer(base_config)._build_app())
    assert paths == ["/mcp/vision/explain"]


def test_advertised_ws_url_defaults_to_nilo_route(base_config):
    server = SimpleHttpServer(base_config)
    assert server._get_websocket_url("192.168.1.2", 8000) == "ws://192.168.1.2:8000/nilo/v1/"
    base_config["server"]["websocket"] = "wss://robots.example.com/nilo/v1/"
    assert SimpleHttpServer(base_config)._get_websocket_url("x", 1) == "wss://robots.example.com/nilo/v1/"
