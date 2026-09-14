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
        ("GET", "/api/robots"),
        ("GET", f"/api/robots/{ROBOT_ID}"),
        ("GET", f"/api/robots/{ROBOT_ID}/memory"),
        ("GET", f"/api/robots/{ROBOT_ID}/memory/recall"),
        ("GET", f"/api/robots/{ROBOT_ID}/behavior"),
        ("POST", f"/api/robots/{ROBOT_ID}/memory/consolidate"),
        ("DELETE", f"/api/robots/{ROBOT_ID}/memory"),
        ("DELETE", f"/api/robots/{ROBOT_ID}/people/ahmad"),
        ("DELETE", f"/api/robots/{ROBOT_ID}/memory/episodic/anything"),
    ],
)
async def test_every_endpoint_but_health_requires_the_admin_token(api, method: str, path: str):
    client, _ = api
    response = await client.request(method, path)
    assert response.status == 401


async def test_a_wrong_token_is_refused(api):
    client, _ = api
    response = await client.get("/api/robots", headers={"Authorization": "Bearer not-the-token"})
    assert response.status == 401


async def test_an_api_with_no_token_configured_fails_closed(runtime):
    async def memories(robot_id: str):
        return None

    app = build_app(runtime, memories, token=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/health")).status == 200
        assert (await client.get("/api/meta")).status == 200
        assert (await client.get("/api/robots")).status == 503
    finally:
        await client.close()


# -- reading ------------------------------------------------------------------------------------------


async def test_robots_are_listed(api):
    client, _ = api
    payload = await (await client.get("/api/robots", headers=AUTH)).json()
    assert [entry["robot_id"] for entry in payload["robots"]] == [ROBOT_ID]
    assert payload["robots"][0]["connected"] is True


async def test_one_robot_reports_its_memory_counts(api):
    client, _ = api
    payload = await (await client.get(f"/api/robots/{ROBOT_ID}", headers=AUTH)).json()
    assert payload["robot_id"] == ROBOT_ID
    assert payload["connected"] is True
    assert payload["memory"]["episodes"] == 2  # the meeting and the play
    assert payload["memory"]["people"] == 1


async def test_memories_are_listed_by_kind(api):
    client, _ = api
    payload = await (await client.get(f"/api/robots/{ROBOT_ID}/memory", headers=AUTH)).json()
    assert {e["summary"] for e in payload["episodes"]} == {"met Ahmad", "played with the cube"}
    assert payload["facts"][0]["value"] == "Nilo"
    assert payload["facts"][0]["provenance"]["learned_from"] == "operator"
    assert payload["people"][0]["display_name"] == "Ahmad"
    assert payload["working"] == ["ahmad: what shall we do?"]

    only_people = await (
        await client.get(f"/api/robots/{ROBOT_ID}/memory?kind=person", headers=AUTH)
    ).json()
    assert "episodes" not in only_people
    assert only_people["people"]


async def test_recall_shows_what_would_go_into_a_prompt(api):
    client, _ = api
    response = await client.get(
        f"/api/robots/{ROBOT_ID}/memory/recall?q=cube&person=ahmad", headers=AUTH
    )
    payload = await response.json()
    assert payload["results"]
    assert all("reasons" in item and item["reasons"] for item in payload["results"])
    assert payload["approx_tokens"] > 0
    assert payload["approx_tokens"] <= 400  # the budget, visible in the response


async def test_why_explains_the_behaviour_decision(api):
    client, _ = api
    payload = await (await client.get(f"/api/robots/{ROBOT_ID}/behavior?fresh=true", headers=AUTH)).json()
    assert payload["robot_id"] == ROBOT_ID
    assert "selected" in payload
    assert "alternatives" in payload


async def test_an_unknown_robot_is_a_404(api):
    client, _ = api
    assert (await client.get("/api/robots/nobody/memory", headers=AUTH)).status == 404


async def test_a_bad_parameter_is_a_400_not_a_500(api):
    client, _ = api
    response = await client.get(f"/api/robots/{ROBOT_ID}/memory?limit=lots", headers=AUTH)
    assert response.status == 400


# -- writing and deleting -------------------------------------------------------------------------------


async def test_consolidation_can_be_triggered(api):
    client, memory = api
    for _ in range(3):
        await memory.met_person("ahmad", display_name="Ahmad")
    payload = await (
        await client.post(f"/api/robots/{ROBOT_ID}/memory/consolidate", headers=AUTH)
    ).json()
    assert payload["episodes_read"] > 0
    assert payload["facts_written"] >= 1
    assert any("familiar" in text for text in payload["facts"])


async def test_one_memory_can_be_deleted_and_deleting_it_twice_is_a_404(api):
    client, memory = api
    episode = (await memory.episodes())[0]
    response = await client.delete(
        f"/api/robots/{ROBOT_ID}/memory/{MemoryKind.EPISODIC.value}/{episode.id}", headers=AUTH
    )
    assert response.status == 200
    assert (await response.json())["deleted"] is True
    again = await client.delete(
        f"/api/robots/{ROBOT_ID}/memory/{MemoryKind.EPISODIC.value}/{episode.id}", headers=AUTH
    )
    assert again.status == 404


async def test_an_unknown_memory_kind_is_refused(api):
    client, _ = api
    response = await client.delete(f"/api/robots/{ROBOT_ID}/memory/dreams/abc", headers=AUTH)
    assert response.status == 400


async def test_deleting_a_person_removes_everything_about_them(api):
    client, memory = api
    response = await client.delete(f"/api/robots/{ROBOT_ID}/people/ahmad", headers=AUTH)
    assert response.status == 200
    assert (await response.json())["rows"] >= 2
    assert await memory.person("ahmad") is None
    assert await memory.episodes(person_id="ahmad") == []


async def test_a_robot_memory_can_be_cleared(api):
    client, memory = api
    response = await client.delete(f"/api/robots/{ROBOT_ID}/memory", headers=AUTH)
    assert response.status == 200
    assert (await response.json())["cleared"] is True
    assert await memory.counts() == {"episodes": 0, "facts": 0, "people": 0, "working": 0}


# -- what the API is not ------------------------------------------------------------------------------------


async def test_no_route_reaches_a_device_except_through_the_executor(api):
    """The API can move a robot now. It still has no second path to the hardware.

    Every control route builds a typed action and submits it to the same executor, which
    is the only thing in the process that calls a device. This is the mechanical check:
    nothing under ``robot/api`` calls ``call_tool``, and nothing imports the safety policy
    it would have to get past (``tests/robot/test_layering.py`` enforces the second half).
    """
    import pathlib

    for path in sorted((pathlib.Path(__file__).resolve().parents[2] / "robot/api").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "call_tool" not in source, f"{path.name} talks to a device directly"
        assert "RobotSafetyPolicy" not in source, f"{path.name} reaches past the action layer"


async def test_the_admin_token_comes_from_the_environment_and_fails_closed():
    from robot.api import ADMIN_TOKEN_ENV, admin_token_from_env

    assert admin_token_from_env({ADMIN_TOKEN_ENV: "  secret  "}) == "secret"
    assert admin_token_from_env({}) is None
    assert admin_token_from_env({ADMIN_TOKEN_ENV: "   "}) is None
