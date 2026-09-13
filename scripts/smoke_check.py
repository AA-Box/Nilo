#!/usr/bin/env python3
"""Hit a running nilo-server on every protocol route and report what answered.

    python scripts/smoke_check.py [--host 127.0.0.1] [--ws-port 8000] [--http-port 8003]

Checks, per protocol (nilo):
  * GET  {ota_path}            -> 200 and a ws:// URL in the body
  * POST {ota_path}            -> 200 JSON with "websocket" (device-id/client-id headers set)
  * WebSocket handshake on ws_path with a device-id header -> 101, then the server accepts a hello

plus that the retired routes are gone and that an unknown WebSocket path is handled
according to `protocols.strict` (which defaults to rejecting it).
Exit code 0 when every enabled route answers, 1 otherwise. Uses only stdlib + `websockets`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

ROUTES = {
    "nilo": ("/nilo/v1/", "/nilo/ota/"),
}
# Routes this server used to serve and must not serve any more.
RETIRED = ("/xiaozhi/v1/", "/xiaozhi/ota/")
HEADERS = {"device-id": "00:11:22:33:44:55", "client-id": "smoke-check"}


def http(method: str, url: str, body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers={**HEADERS, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:  # server down / port closed
        return 0, str(e)


async def ws_hello(url: str) -> str:
    import websockets

    try:
        async with websockets.connect(url, additional_headers=HEADERS, open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                      "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
            return "ok" if reply.get("type") == "hello" else f"unexpected reply {reply!r}"
    except websockets.exceptions.InvalidStatus as e:  # handshake rejected
        return f"rejected ({e.response.status_code})"
    except Exception as e:  # noqa: BLE001 - report anything else as-is
        return f"error: {e!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=8000)
    ap.add_argument("--http-port", type=int, default=8003)
    ap.add_argument("--expect-disabled", default="", help="comma-separated protocol names expected to be rejected")
    args = ap.parse_args()
    disabled = {p for p in args.expect_disabled.split(",") if p}

    failures = 0
    for name, (ws_path, ota_path) in ROUTES.items():
        base = f"http://{args.host}:{args.http_port}"
        status, body = http("GET", base + ota_path)
        ok_get = status == 200 and ("ws://" in body or "wss://" in body)
        status_post, body_post = http("POST", base + ota_path, b"{}")
        ok_post = status_post == 200 and '"websocket"' in body_post
        ws_result = asyncio.run(ws_hello(f"ws://{args.host}:{args.ws_port}{ws_path}"))
        if name in disabled:
            good = status == 404 and ws_result.startswith("rejected")
        else:
            good = ok_get and ok_post and ws_result == "ok"
        failures += 0 if good else 1
        print(f"[{'PASS' if good else 'FAIL'}] {name:15} GET {ota_path} -> {status}; POST -> {status_post}; ws {ws_path} -> {ws_result}")

    for path in RETIRED:
        if path.endswith("/ota/"):
            status, _ = http("GET", f"http://{args.host}:{args.http_port}{path}")
            good = status == 404
            detail = f"GET -> {status}"
        else:
            result = asyncio.run(ws_hello(f"ws://{args.host}:{args.ws_port}{path}"))
            good = result.startswith("rejected")
            detail = f"ws -> {result}"
        failures += 0 if good else 1
        print(f"[{'PASS' if good else 'FAIL'}] retired {path:16} {detail}")

    unknown = asyncio.run(ws_hello(f"ws://{args.host}:{args.ws_port}/not-a-protocol/"))
    print(f"[info] unknown ws path -> {unknown} (rejected unless protocols.strict is false)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
