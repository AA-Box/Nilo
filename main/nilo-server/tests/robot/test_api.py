"""The robot admin API: what it serves, what it refuses, and what it cannot do.

Runs against a real aiohttp application through ``aiohttp.test_utils``; skipped entirely
in the dev dependency slice, which does not install aiohttp — the same convention the
inherited HTTP tests use.
"""
from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from robot.api.server import build_app  # noqa: E402
from robot.memory.models import EventType, MemoryKind  # noqa: E402
from robot.memory.service import RobotMemory  # noqa: E402
from robot.memory.store import IN_MEMORY, SqliteMemoryStore  # noqa: E402
from robot.state.models import DeviceInfo  # noqa: E402
from tests.robot.conftest import ROBOT_ID  # noqa: E402

TOKEN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api(runtime):
    """A live admin API over a runtime with one registered robot and a real memory."""
    store = SqliteMemoryStore(IN_MEMORY)
    memory = RobotMemory(ROBOT_ID, store)
    await memory.open()
    await runtime.attach(DeviceInfo(device_id=ROBOT_ID, session_id="session-1"), discover=False)

    await memory.met_person("ahmad", display_name="Ahmad")
    await memory.remember("played with the cube", event_type=EventType.PLAY, person_id="ahmad", about="cube")
    await memory.learn("robot", "name", "Nilo", confidence=0.95, learned_from="operator")
    memory.working.add("what shall we do?", person_id="ahmad")

    async def memories(robot_id: str):
        return memory if robot_id == ROBOT_ID else None

    app = build_app(runtime, memories, token=TOKEN)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, memory
    finally:
        await client.close()
        await memory.aclose()
        await store.aclose()


# -- auth ---------------------------------------------------------------------------------------


async def test_health_needs_no_token_and_leaks_nothing(api):
    client, _ = api
    response = await client.get("/health")
    assert response.status == 200
    payload = await response.json()
    assert payload["status"] == "ok"
    assert set(payload) == {"status", "robots"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/robots"),
        ("GET", f"/robots/{ROBOT_ID}"),
        ("GET", f"/robots/{ROBOT_ID}/memories"),
        ("GET", f"/robots/{ROBOT_ID}/memories/recall"),
        ("GET", f"/robots/{ROBOT_ID}/why"),
        ("POST", f"/robots/{ROBOT_ID}/memories/consolidate"),
        ("DELETE", f"/robots/{ROBOT_ID}/memories"),
        ("DELETE", f"/robots/{ROBOT_ID}/people/ahmad"),
        ("DELETE", f"/robots/{ROBOT_ID}/memories/episodic/anything"),
    ],
)
async def test_every_endpoint_but_health_requires_the_admin_token(api, method: str, path: str):
    client, _ = api
    response = await client.request(method, path)
    assert response.status == 401


async def test_a_wrong_token_is_refused(api):
    client, _ = api
    response = await client.get("/robots", headers={"Authorization": "Bearer not-the-token"})
    assert response.status == 401


async def test_an_api_with_no_token_configured_fails_closed(runtime):
    async def memories(robot_id: str):
        return None

    app = build_app(runtime, memories, token=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 200
        assert (await client.get("/robots")).status == 503
    finally:
        await client.close()


# -- reading ------------------------------------------------------------------------------------------


async def test_robots_are_listed(api):
    client, _ = api
    payload = await (await client.get("/robots", headers=AUTH)).json()
    assert payload["robots"] == [ROBOT_ID]


async def test_one_robot_reports_its_memory_counts(api):
    client, _ = api
    payload = await (await client.get(f"/robots/{ROBOT_ID}", headers=AUTH)).json()
    assert payload["robot_id"] == ROBOT_ID
    assert payload["connected"] is True
    assert payload["memory"]["episodes"] == 2  # the meeting and the play
    assert payload["memory"]["people"] == 1


async def test_memories_are_listed_by_kind(api):
    client, _ = api
    payload = await (await client.get(f"/robots/{ROBOT_ID}/memories", headers=AUTH)).json()
    assert {e["summary"] for e in payload["episodes"]} == {"met Ahmad", "played with the cube"}
    assert payload["facts"][0]["value"] == "Nilo"
    assert payload["facts"][0]["provenance"]["learned_from"] == "operator"
    assert payload["people"][0]["display_name"] == "Ahmad"
    assert payload["working"] == ["ahmad: what shall we do?"]

    only_people = await (
        await client.get(f"/robots/{ROBOT_ID}/memories?kind=person", headers=AUTH)
    ).json()
    assert "episodes" not in only_people
    assert only_people["people"]


async def test_recall_shows_what_would_go_into_a_prompt(api):
    client, _ = api
    response = await client.get(
        f"/robots/{ROBOT_ID}/memories/recall?q=cube&person=ahmad", headers=AUTH
    )
    payload = await response.json()
    assert payload["results"]
    assert all("reasons" in item and item["reasons"] for item in payload["results"])
    assert payload["approx_tokens"] > 0
    assert payload["approx_tokens"] <= 400  # the budget, visible in the response


async def test_why_explains_the_behaviour_decision(api):
    client, _ = api
    payload = await (await client.get(f"/robots/{ROBOT_ID}/why?fresh=true", headers=AUTH)).json()
    assert payload["robot_id"] == ROBOT_ID
    assert "selected" in payload
    assert "alternatives" in payload


async def test_an_unknown_robot_is_a_404(api):
    client, _ = api
    assert (await client.get("/robots/nobody/memories", headers=AUTH)).status == 404


async def test_a_bad_parameter_is_a_400_not_a_500(api):
    client, _ = api
    response = await client.get(f"/robots/{ROBOT_ID}/memories?limit=lots", headers=AUTH)
    assert response.status == 400


# -- writing and deleting -------------------------------------------------------------------------------


async def test_consolidation_can_be_triggered(api):
    client, memory = api
    for _ in range(3):
        await memory.met_person("ahmad", display_name="Ahmad")
    payload = await (
        await client.post(f"/robots/{ROBOT_ID}/memories/consolidate", headers=AUTH)
    ).json()
    assert payload["episodes_read"] > 0
    assert payload["facts_written"] >= 1
    assert any("familiar" in text for text in payload["facts"])


async def test_one_memory_can_be_deleted_and_deleting_it_twice_is_a_404(api):
    client, memory = api
    episode = (await memory.episodes())[0]
    response = await client.delete(
        f"/robots/{ROBOT_ID}/memories/{MemoryKind.EPISODIC.value}/{episode.id}", headers=AUTH
    )
    assert response.status == 200
    assert (await response.json())["deleted"] is True
    again = await client.delete(
        f"/robots/{ROBOT_ID}/memories/{MemoryKind.EPISODIC.value}/{episode.id}", headers=AUTH
    )
    assert again.status == 404


async def test_an_unknown_memory_kind_is_refused(api):
    client, _ = api
    response = await client.delete(f"/robots/{ROBOT_ID}/memories/dreams/abc", headers=AUTH)
    assert response.status == 400


async def test_deleting_a_person_removes_everything_about_them(api):
    client, memory = api
    response = await client.delete(f"/robots/{ROBOT_ID}/people/ahmad", headers=AUTH)
    assert response.status == 200
    assert (await response.json())["rows"] >= 2
    assert await memory.person("ahmad") is None
    assert await memory.episodes(person_id="ahmad") == []


async def test_a_robot_memory_can_be_cleared(api):
    client, memory = api
    response = await client.delete(f"/robots/{ROBOT_ID}/memories", headers=AUTH)
    assert response.status == 200
    assert (await response.json())["cleared"] is True
    assert await memory.counts() == {"episodes": 0, "facts": 0, "people": 0, "working": 0}


# -- what the API is not ------------------------------------------------------------------------------------


async def test_there_is_no_endpoint_that_moves_a_robot(api):
    """Actuation belongs to the action layer, behind the safety policy. An admin surface
    that could drive a robot would be a second path to the hardware."""
    client, _ = api
    app = client.app
    paths = {resource.canonical for resource in app.router.resources()}
    for path in paths:
        assert not any(word in path for word in ("move", "turn", "drive", "action", "stop", "actuator"))


async def test_the_admin_token_comes_from_the_environment_and_fails_closed():
    from robot.api.server import ADMIN_TOKEN_ENV, admin_token_from_env

    assert admin_token_from_env({ADMIN_TOKEN_ENV: "  secret  "}) == "secret"
    assert admin_token_from_env({}) is None
    assert admin_token_from_env({ADMIN_TOKEN_ENV: "   "}) is None
