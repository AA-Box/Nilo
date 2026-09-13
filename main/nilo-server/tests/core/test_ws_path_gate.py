"""WebSocketServer.process_request hook: accepts enabled protocol paths, rejects disabled ones."""
import asyncio
import logging

import pytest

pytest.importorskip("websockets")  # full runtime dependency; skipped on the dev-only slice
pytest.importorskip("opuslib_next")
pytest.importorskip("numpy")

from core.websocket_server import WebSocketServer  # noqa: E402
from robot.protocol import registry_from_config


class _Req:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers


class _Conn:
    def respond(self, status, text):
        return (status, text)


def _server(config):
    srv = WebSocketServer.__new__(WebSocketServer)  # skip __init__: it loads VAD/ASR models
    srv.protocols = registry_from_config(config)

    class _L:
        def bind(self, **_):
            return logging.getLogger("test")

    srv.logger = _L()
    return srv


def _gate(srv, path, connection="Upgrade"):
    return asyncio.run(srv._http_response(_Conn(), _Req(path, {"connection": connection})))


def test_nilo_path_accepted():
    srv = _server({})
    assert _gate(srv, "/nilo/v1/") is None
    assert _gate(srv, "/nilo/v1/?device-id=11:22", "keep-alive, Upgrade") is None


def test_retired_and_unknown_paths_rejected():
    srv = _server({})
    assert _gate(srv, "/xiaozhi/v1/")[0] == 404
    assert _gate(srv, "/anything/")[0] == 404


def test_non_strict_accepts_unknown_paths():
    srv = _server({"protocols": {"strict": False}})
    assert _gate(srv, "/anything/") is None


def test_plain_http_probe_gets_200():
    status, text = _gate(_server({}), "/", connection="keep-alive")
    assert status == 200 and "nilo-server" in text
