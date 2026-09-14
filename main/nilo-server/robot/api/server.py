"""A small admin API: inspect what the robot remembers, and why it is doing things.

    from robot.api import build_app, run_api

    app = build_app(runtime, memories, token="…")     # aiohttp application
    await run_api(runtime, memories, token="…", port=8010)

Four rules, each of them from robot-architecture §4.2 and §4.3:

* **Its own aiohttp app on its own port.** Routes are never added to
  ``core/http_server.py``: that table is built from the protocol registry and serves
  *device* traffic, and every route added there is an inherited-file edit plus an admin
  surface on the device port.
* **Its own credential.** The admin token is distinct from the device-token signing key.
  The OTA endpoint is unauthenticated and, with auth enabled, will mint a valid token for
  whatever device id the caller asks for — so "the caller holds a valid device token"
  authorizes nothing here (docs/safety.md).
* **Read-mostly, and explicit about the rest.** Everything that deletes is a ``DELETE``,
  requires the token, and says exactly how many rows it removed.
* **aiohttp is imported lazily.** ``import robot.api`` costs nothing in a process that
  will never serve, and the dev test slice runs without aiohttp installed.

This API is deliberately *not* a control surface: there is no endpoint here that moves a
robot. Actuation belongs to the action layer, behind the safety policy, and an admin
surface that could drive a robot would be a second path to the hardware.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from robot.memory.models import MemoryKind
from robot.memory.service import RobotMemory

logger = logging.getLogger(__name__)

#: The port the admin API listens on when nothing says otherwise. Not the device port.
DEFAULT_API_PORT = 8010

#: Environment variable the admin token is read from. Not the server config dict: in
#: manager-api mode the local configuration is replaced wholesale by the API response
#: (``config/config_loader.py``), and a credential that vanishes in the deployment with
#: the most robots in it is not a credential.
ADMIN_TOKEN_ENV = "NILO_ROBOT_ADMIN_TOKEN"

#: Where a memory for one robot comes from. The runtime supplies this; a test supplies a
#: dictionary lookup.
MemorySource = Callable[[str], Awaitable[RobotMemory | None]]


def admin_token_from_env(env: dict[str, str] | None = None) -> str | None:
    """The admin token from the environment, or ``None`` if it is not set.

    ``None`` means the API refuses every authenticated route (it fails closed), which is
    the right behaviour for a surface that can delete a robot's memory.
    """
    source = os.environ if env is None else env
    token = source.get(ADMIN_TOKEN_ENV, "").strip()
    if not token:
        logger.warning(
            "%s is not set; the robot admin API will refuse every authenticated request",
            ADMIN_TOKEN_ENV,
        )
        return None
    return token


class ApiError(Exception):
    """An error with an HTTP status. Turned into a JSON body by the handler wrapper."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def build_app(
    runtime: Any,
    memories: MemorySource,
    *,
    token: str | None = None,
    require_auth: bool = True,
) -> Any:
    """The aiohttp application. Imported lazily so this module loads without aiohttp.

    ``token`` is the admin credential. With ``require_auth`` on (the default) and no
    token configured, every route except ``/health`` refuses — a misconfigured admin API
    must fail closed, not open.
    """
    from aiohttp import web  # noqa: PLC0415 - deliberately lazy: see the module docstring

    routes = web.RouteTableDef()

    def authorize(request: web.Request) -> None:
        if not require_auth:
            return
        if not token:
            raise ApiError(503, "the robot admin API has no token configured")
        header = request.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time: a token check that leaks its comparison time is a token check
        # somebody can walk one character at a time.
        if not supplied or not hmac.compare_digest(supplied, token):
            raise ApiError(401, "a valid admin token is required")

    async def memory_for(robot_id: str) -> RobotMemory:
        memory = await memories(robot_id)
        if memory is None:
            raise ApiError(404, f"no memory for robot {robot_id!r}")
        return memory

    @routes.get("/health")
    async def health(request: web.Request) -> web.Response:
        """Unauthenticated on purpose: a liveness probe is not an admin operation, and it
        returns nothing about any robot."""
        return web.json_response({"status": "ok", "robots": len(await _robot_ids(runtime))})

    @routes.get("/robots")
    async def list_robots(request: web.Request) -> web.Response:
        authorize(request)
        return web.json_response({"robots": await _robot_ids(runtime)})

    @routes.get("/robots/{robot_id}")
    async def robot_detail(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = request.match_info["robot_id"]
        state = await runtime.get_state(robot_id) if hasattr(runtime, "get_state") else None
        memory = await memories(robot_id)
        return web.json_response(
            {
                "robot_id": robot_id,
                "connected": bool(state.is_connected) if state is not None else False,
                "memory": await memory.counts() if memory is not None else {},
            }
        )

    @routes.get("/robots/{robot_id}/why")
    async def why(request: web.Request) -> web.Response:
        """Why is the robot doing this? The behaviour engine's last decision, as JSON."""
        authorize(request)
        robot_id = request.match_info["robot_id"]
        if not hasattr(runtime, "behavior"):
            raise ApiError(501, "this runtime has no behaviour engine")
        fresh = request.query.get("fresh", "").lower() in {"1", "true", "yes"}
        return web.json_response(runtime.behavior(robot_id).explain_data(fresh=fresh))

    @routes.get("/robots/{robot_id}/memories")
    async def list_memories(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        limit = _int_param(request, "limit", 50, low=1, high=500)
        person_id = request.query.get("person")
        kind = request.query.get("kind", "")
        payload: dict[str, Any] = {"robot_id": memory.robot_id, "counts": await memory.counts()}
        if kind in ("", MemoryKind.EPISODIC.value):
            episodes = await memory.episodes(limit=limit, person_id=person_id)
            payload["episodes"] = [_episode_json(episode) for episode in episodes]
        if kind in ("", MemoryKind.SEMANTIC.value):
            payload["facts"] = [_fact_json(fact) for fact in await memory.facts(subject=person_id, limit=limit)]
        if kind in ("", MemoryKind.PERSON.value):
            payload["people"] = [_person_json(person) for person in await memory.people(limit=limit)]
        if kind in ("", MemoryKind.WORKING.value):
            payload["working"] = [item.line for item in memory.working.recent()]
        return web.json_response(payload)

    @routes.get("/robots/{robot_id}/memories/recall")
    async def recall(request: web.Request) -> web.Response:
        """What the robot would actually put in front of a model for this question."""
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        query = request.query.get("q", "")
        person_id = request.query.get("person")
        selected = await memory.recall(query, person_id=person_id)
        return web.json_response(
            {
                "query": query,
                "person_id": person_id,
                "results": [
                    {
                        "kind": item.kind.value,
                        "id": item.id,
                        "text": item.text,
                        "score": item.score,
                        "reasons": list(item.reasons),
                        "approx_tokens": item.approx_tokens,
                    }
                    for item in selected
                ],
                "approx_tokens": sum(item.approx_tokens for item in selected),
            }
        )

    @routes.post("/robots/{robot_id}/memories/consolidate")
    async def consolidate(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        result = await memory.consolidate()
        return web.json_response(
            {
                "episodes_read": result.episodes_read,
                "facts_written": result.facts_written,
                "facts": [fact.text for fact in result.facts],
            }
        )

    @routes.delete("/robots/{robot_id}/memories/{kind}/{memory_id}")
    async def delete_memory(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        try:
            kind = MemoryKind(request.match_info["kind"])
        except ValueError:
            raise ApiError(400, f"unknown memory kind {request.match_info['kind']!r}") from None
        removed = await memory.forget(kind, request.match_info["memory_id"])
        if not removed:
            raise ApiError(404, "no such memory")
        return web.json_response({"deleted": True, "kind": kind.value, "id": request.match_info["memory_id"]})

    @routes.delete("/robots/{robot_id}/people/{person_id}")
    async def delete_person(request: web.Request) -> web.Response:
        """Delete a person and everything about them: the record, their episodes, their facts."""
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        removed = await memory.forget_person(request.match_info["person_id"])
        return web.json_response({"deleted": removed > 0, "rows": removed})

    @routes.delete("/robots/{robot_id}/memories")
    async def clear_memory(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(request.match_info["robot_id"])
        removed = await memory.clear()
        return web.json_response({"cleared": True, "rows": removed})

    @web.middleware
    async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            # Annotated rather than returned directly: `handler` is untyped, and mypy is
            # strict for robot/*. It is also Any when aiohttp is not installed at all,
            # which is how the dev test slice type-checks this file.
            response: web.StreamResponse = await handler(request)
            return response
        except ApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.exception("robot admin API: %s %s failed", request.method, request.path)
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    app = web.Application(middlewares=[errors])
    app.add_routes(routes)
    return app


async def run_api(
    runtime: Any,
    memories: MemorySource,
    *,
    token: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_API_PORT,
    require_auth: bool = True,
) -> Any:
    """Start the admin API and return its runner. The caller owns shutdown.

    Binds to loopback by default: an admin API that can delete a robot's memory should not
    become reachable from the network because somebody forgot to set a host.
    """
    from aiohttp import web  # noqa: PLC0415

    app = build_app(runtime, memories, token=token, require_auth=require_auth)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("robot admin API listening on http://%s:%d", host, port)
    return runner


def supervise(task: asyncio.Task[Any], on_failure: Callable[[BaseException], None]) -> None:
    """Escalate when the API task dies instead of letting it fail silently.

    ``app.py`` creates its tasks and never inspects them, so a port conflict degrades the
    server quietly (robot-architecture R5). Anything that starts this API attaches this,
    and the robot subsystem finds out.
    """

    def done(finished: asyncio.Task[Any]) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            logger.error("robot admin API task failed: %s", error)
            on_failure(error)

    task.add_done_callback(done)


# -- helpers ---------------------------------------------------------------------------------


async def _robot_ids(runtime: Any) -> list[str]:
    states = await runtime.states.list_states() if hasattr(runtime, "states") else []
    return sorted(state.robot_id for state in states)


def _int_param(request: Any, name: str, default: int, *, low: int, high: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        raise ApiError(400, f"{name} must be an integer") from None


def _episode_json(episode: Any) -> dict[str, Any]:
    return {
        "id": episode.id,
        "event_type": episode.event_type.value,
        "summary": episode.summary,
        "timestamp": episode.timestamp.isoformat(),
        "person_id": episode.person_id,
        "importance": episode.importance,
        "consolidated": episode.consolidated_at is not None,
    }


def _fact_json(fact: Any) -> dict[str, Any]:
    return {
        "id": fact.id,
        "subject": fact.subject,
        "predicate": fact.predicate,
        "value": fact.value,
        "confidence": fact.confidence,
        "provenance": {
            "learned_from": fact.provenance.learned_from,
            "source_ids": list(fact.provenance.source_ids),
            "created_at": fact.provenance.created_at.isoformat(),
            "updated_at": fact.provenance.updated_at.isoformat(),
            "confirmations": fact.provenance.confirmations,
        },
    }


def _person_json(person: Any) -> dict[str, Any]:
    return {
        "person_id": person.person_id,
        "display_name": person.display_name,
        "first_seen": person.first_seen.isoformat(),
        "last_seen": person.last_seen.isoformat(),
        "interaction_count": person.interaction_count,
        "familiarity": round(person.familiarity_now(), 4),
        "embedding_ref": person.embedding_ref,
        "facts": person.facts,
    }


__all__ = [
    "ADMIN_TOKEN_ENV",
    "DEFAULT_API_PORT",
    "ApiError",
    "MemorySource",
    "admin_token_from_env",
    "build_app",
    "run_api",
    "supervise",
]
