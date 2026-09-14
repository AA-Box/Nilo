"""The live event stream, the OpenAPI document, and the dashboard that reads both.

The stream is the part of the management API that is easy to get subtly wrong: a filter
that silently matches nothing, a new event type that never appears because somebody forgot
a mapping, or a slow reader that stalls the publisher. Each of those has a test here.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from robot.api.events import ALL_CATEGORIES, CATEGORIES, EventStream, category_of, encode, parse_categories  # noqa: E402
from robot.api.openapi import document  # noqa: E402
from robot.api.security import ApiSecurity  # noqa: E402
from robot.api.server import build_app  # noqa: E402
from robot.events import types as event_types  # noqa: E402
from robot.runtime import RobotRuntime  # noqa: E402
from tests.robot.conftest import ROBOT_ID  # noqa: E402
from tests.robot.simulated import SimulatedDevice  # noqa: E402

TOKEN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api():
    runtime = RobotRuntime(discovery_timeout=1.0)
    device = await SimulatedDevice(ROBOT_ID).attach(runtime)

    async def memories(robot_id: str):
        return None

    app = build_app(runtime, memories, security=ApiSecurity(token=TOKEN))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, runtime, device
    finally:
        await client.close()
        await device.aclose()
        await runtime.aclose()


async def read_frames(response, count: int, timeout: float = 5.0) -> list[dict]:
    """Read SSE frames until ``count`` data frames have arrived, or time out."""
    frames: list[dict] = []
    buffer = ""

    async def pump() -> None:
        nonlocal buffer
        while len(frames) < count:
            chunk = await response.content.read(512)
            if not chunk:
                return
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))

    await asyncio.wait_for(pump(), timeout=timeout)
    return frames


# -- the stream ----------------------------------------------------------------------------------


async def test_the_stream_needs_the_token(api):
    client, _, _ = api
    assert (await client.get("/api/events")).status == 401


async def test_events_reach_a_subscriber(api):
    client, runtime, _ = api
    # Filtered, because the simulated robot is busy reporting telemetry and this test is
    # about delivery rather than about what happens to be happening.
    response = await client.get("/api/events?categories=conversation", headers=AUTH)
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    async def emit() -> None:
        await asyncio.sleep(0.05)
        await runtime.events.publish(
            event_types.AudioStateChanged(robot_id=ROBOT_ID, previous="idle", state="listening")
        )

    task = asyncio.create_task(emit())
    frames = await read_frames(response, 1)
    await task
    response.close()

    assert frames[0]["event"] == "AudioStateChanged"
    assert frames[0]["category"] == "conversation"
    assert frames[0]["state"] == "listening"


async def test_a_category_filter_only_delivers_that_category(api):
    client, runtime, _ = api
    response = await client.get("/api/events?categories=conversation", headers=AUTH)

    async def emit() -> None:
        await asyncio.sleep(0.05)
        await runtime.events.publish(event_types.BehaviorStarted(robot_id=ROBOT_ID, behavior="idle_look"))
        await runtime.events.publish(
            event_types.UtteranceRecognized(robot_id=ROBOT_ID, text="hello")
        )

    task = asyncio.create_task(emit())
    frames = await read_frames(response, 1)
    await task
    response.close()

    assert [frame["event"] for frame in frames] == ["UtteranceRecognized"]


async def test_a_robot_filter_only_delivers_that_robot(api):
    client, runtime, _ = api
    response = await client.get("/api/events?robot=someone-else", headers=AUTH)

    async def emit() -> None:
        await asyncio.sleep(0.05)
        await runtime.events.publish(event_types.AudioStateChanged(robot_id=ROBOT_ID, state="listening"))
        await runtime.events.publish(
            event_types.AudioStateChanged(robot_id="someone-else", state="speaking")
        )

    task = asyncio.create_task(emit())
    frames = await read_frames(response, 1)
    await task
    response.close()

    assert [frame["robot_id"] for frame in frames] == ["someone-else"]


async def test_an_unknown_category_is_a_400_not_an_empty_stream(api):
    """A dashboard that asked for "behaviour" and got nothing would look like a broken robot."""
    client, _, _ = api
    response = await client.get("/api/events?categories=behaviour", headers=AUTH)
    assert response.status == 400
    assert "behaviour" in (await response.json())["error"]


async def test_a_slow_reader_loses_its_oldest_events_rather_than_stalling_the_publisher():
    stream = EventStream(queue_size=3)
    for index in range(10):
        stream.offer(event_types.AudioStateChanged(robot_id=ROBOT_ID, detail=str(index)))

    assert stream.queue.qsize() == 3
    assert stream.dropped == 7
    newest = [stream.queue.get_nowait().detail for _ in range(3)]
    assert newest == ["7", "8", "9"], "the newest events were dropped instead of the oldest"


async def test_every_event_type_has_a_category():
    """A new event type shows up in the stream on the day it is added, not the day
    somebody remembers to map it."""
    for name in dir(event_types):
        candidate = getattr(event_types, name)
        if not isinstance(candidate, type) or not issubclass(candidate, event_types.RobotEvent):
            continue
        if candidate is event_types.RobotEvent:
            continue
        assert CATEGORIES.get(name, "system") in ALL_CATEGORIES


def test_an_unmapped_event_is_system_rather_than_dropped():
    class Invented(event_types.RobotEvent):
        pass

    assert category_of(Invented(robot_id=ROBOT_ID)) == "system"


def test_a_frame_is_well_formed_sse():
    frame = encode(event_types.AudioStateChanged(robot_id=ROBOT_ID, state="speaking"))
    assert frame.startswith("id: ")
    assert "\nevent: conversation\n" in frame
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["robot_id"] == ROBOT_ID


def test_no_filter_means_everything():
    assert parse_categories(None) is None
    assert parse_categories("") is None
    assert parse_categories("action,world") == frozenset({"action", "world"})


# -- documentation and the dashboard ----------------------------------------------------------------


async def test_every_route_the_app_registers_is_documented(api):
    client, _, _ = api
    spec = await (await client.get("/api/openapi.json")).json()
    documented = set(spec["paths"])

    served = set()
    for resource in client.app.router.resources():
        canonical = resource.canonical
        if canonical == "/":
            continue
        served.add(canonical)

    undocumented = served - documented
    assert not undocumented, f"undocumented routes: {sorted(undocumented)}"


async def test_the_openapi_document_says_what_this_binding_allows(api):
    client, _, _ = api
    spec = await (await client.get("/api/openapi.json")).json()
    assert spec["openapi"].startswith("3.")
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert "local_only" in spec["info"]["description"] or "127.0.0.1" in spec["info"]["description"]


async def test_control_bodies_are_documented_from_the_models():
    spec = document()
    move = spec["paths"]["/api/robots/{robot_id}/actions/move"]["post"]
    schema = move["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["distance_mm"]["type"] == "integer"
    assert schema["additionalProperties"] is False


async def test_the_dashboard_is_served_and_needs_no_network(api):
    client, _, _ = api
    response = await client.get("/")
    assert response.status == 200
    body = await response.text()
    assert "NILO ROBOT DASHBOARD" in body
    # No build step and no third-party origin: one file, served as it is.
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "cdn" not in body.lower()
    assert "<script src=" not in body


async def test_the_dashboard_never_puts_the_token_in_a_url(api):
    """EventSource cannot set a header, so the page uses fetch. A token in a query string
    ends up in browser history and in every access log between here and there."""
    client, _, _ = api
    body = await (await client.get("/")).text()
    assert "?token=" not in body
    assert "new EventSource(" not in body
    assert "Authorization" in body


async def test_the_dashboard_panels_the_design_note_asks_for_are_present(api):
    client, _, _ = api
    body = await (await client.get("/")).text()
    for panel in (
        "Robot",
        "Battery",
        "Sensors",
        "Current action",
        "Behaviour",
        "Candidate scores",
        "Internal state",
        "World entities",
        "Known people",
        "Recent memories",
        "Robot tools",
        "Conversation",
        "Recent events",
        "Simulator",
    ):
        assert panel in body, f"the dashboard has no {panel} panel"
    for control in ("move", "turn", "stop", "head", "lift", "play", "express", "autonomy", "emergency stop"):
        assert control in body, f"the dashboard has no {control} control"
