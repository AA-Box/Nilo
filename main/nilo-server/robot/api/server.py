"""The robot management API: one aiohttp application, on its own port, with its own token.

    from robot.api import build_app, run_api

    app = build_app(runtime, memories, token="…")
    await run_api(runtime, memories, token="…", port=8010)

Four rules, each of them from robot-architecture §4.2 and §4.3:

* **Its own app on its own port.** Routes are never added to ``core/http_server.py``: that
  table is built from the protocol registry and serves *device* traffic, and every route
  added there is an inherited-file edit plus an admin surface on the device port.
* **Its own credential.** The admin token is distinct from the device-token signing key.
  The OTA endpoint is unauthenticated and, with auth enabled, will mint a valid token for
  whatever device id the caller asks for — so "the caller holds a valid device token"
  authorizes nothing here (docs/safety.md).
* **Control is gated three ways.** A token, a loopback rule, and a rate limit
  (:mod:`robot.api.security`). Read endpoints need only the first.
* **aiohttp is imported lazily.** ``import robot.api`` costs nothing in a process that
  will never serve, and the dev test slice runs without aiohttp installed.

**This API can now move a robot**, which is a change from Phase 7, where it deliberately
could not. What has not changed is where the decision is made: every control request
becomes a typed action submitted to the same executor and judged by the same safety policy
as a behaviour's own command (:mod:`robot.api.control`). An operator with the admin token
cannot obtain what the policy would refuse a model, and the emergency stop is a request
the firmware watchdog backs — not a guarantee this process can make (docs/safety.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from robot.agent.permissions import TurnOrigin
from robot.api import control, events, simulate, views
from robot.api.dashboard import page
from robot.api.openapi import document
from robot.api.security import (
    ADMIN_TOKEN_ENV,
    ALLOW_REMOTE_CONTROL_ENV,
    API_HOST_ENV,
    API_PORT_ENV,
    DEFAULT_API_PORT,
    ApiError,
    ApiSecurity,
    Sensitivity,
    admin_token_from_env,
    is_loopback,
)
from robot.memory.models import MemoryKind
from robot.memory.service import RobotMemory

logger = logging.getLogger(__name__)

#: Where a memory for one robot comes from. The runtime supplies this; a test supplies a
#: dictionary lookup.
MemorySource = Callable[[str], Awaitable[RobotMemory | None]]


def build_app(
    runtime: Any,
    memories: MemorySource,
    *,
    token: str | None = None,
    require_auth: bool = True,
    security: ApiSecurity | None = None,
) -> Any:
    """The aiohttp application. Imported lazily so this module loads without aiohttp.

    ``token`` is the admin credential. With ``require_auth`` on (the default) and no token
    configured, every route except ``/health`` and ``/api/meta`` refuses — a misconfigured
    management API must fail closed, not open.
    """
    from aiohttp import web  # noqa: PLC0415 - deliberately lazy: see the module docstring

    guard = security or ApiSecurity(token=token, require_auth=require_auth)
    routes = web.RouteTableDef()

    def authorize(request: web.Request, sensitivity: Sensitivity = Sensitivity.READ) -> None:
        guard.authorize(request, sensitivity)

    async def robot_id_of(request: web.Request) -> str:
        """The robot in the path, checked. A robot nobody has heard of is a 404, not a 500."""
        robot_id = request.match_info["robot_id"]
        if await runtime.get_state(robot_id) is None:
            raise ApiError(404, f"no robot named {robot_id!r}")
        return robot_id

    async def memory_for(robot_id: str) -> RobotMemory:
        memory = await memories(robot_id)
        if memory is None:
            raise ApiError(404, f"no memory for robot {robot_id!r}")
        return memory

    async def body_of(request: web.Request) -> Any:
        if not request.can_read_body:
            return {}
        try:
            return await request.json()
        except Exception:
            raise ApiError(400, "the request body is not valid JSON") from None

    async def controller(request: web.Request) -> control.RobotControl:
        authorize(request, Sensitivity.CONTROL)
        return control.RobotControl(runtime, await robot_id_of(request))

    async def submit(request: web.Request, model: type[Any], operation: str) -> web.Response:
        """The shape every motion endpoint has: authorize, validate, submit, report."""
        robot = await controller(request)
        payload = control.parse(model, await body_of(request))
        record = await getattr(robot, operation)(payload)
        body, status = control.action_response(record)
        return web.json_response(body, status=status)

    # -- public --------------------------------------------------------------------------------

    @routes.get("/health")
    async def health(request: web.Request) -> web.Response:
        """Unauthenticated on purpose: a liveness probe is not an admin operation, and it
        returns nothing about any robot."""
        return web.json_response({"status": "ok", "robots": len(await _robot_ids(runtime))})

    @routes.get("/api/meta")
    async def meta(request: web.Request) -> web.Response:
        """What this binding allows. No robot data, so the dashboard can render its header
        before anybody has typed a token."""
        return web.json_response(guard.describe())

    @routes.get("/")
    async def dashboard(request: web.Request) -> web.Response:
        return web.Response(text=page(), content_type="text/html")

    @routes.get("/api/openapi.json")
    async def openapi(request: web.Request) -> web.Response:
        return web.json_response(document(guard))

    # -- reading -------------------------------------------------------------------------------

    @routes.get("/api/robots")
    async def list_robots(request: web.Request) -> web.Response:
        authorize(request)
        states = await runtime.states.list_states()
        summaries = [views.robot_summary(state, state.robot_id) for state in states]
        summaries.sort(key=lambda entry: entry["robot_id"])
        return web.json_response({"robots": summaries})

    @routes.get("/api/robots/{robot_id}")
    async def robot_detail(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        state = await runtime.get_state(robot_id)
        memory = await memories(robot_id)
        payload = views.robot_summary(state, robot_id)
        payload["memory"] = await memory.counts() if memory is not None else {}
        payload["emergency_stopped"] = runtime.actions.emergency_stopped(robot_id)
        payload["autonomy_mode"] = runtime.autonomy.value
        return web.json_response(payload)

    @routes.get("/api/robots/{robot_id}/state")
    async def robot_state(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        return web.json_response(views.robot_state(await runtime.get_state(robot_id)))

    @routes.get("/api/robots/{robot_id}/capabilities")
    async def capabilities(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        return web.json_response(views.capabilities(await runtime.get_state(robot_id)))

    @routes.get("/api/robots/{robot_id}/actions")
    async def list_actions(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        limit = _int_param(request, "limit", 50, low=1, high=500)
        wanted = request.query.get("status")
        records = runtime.actions.list_actions(robot_id, limit=limit)
        rendered = [views.action(record) for record in records]
        if wanted:
            rendered = [entry for entry in rendered if entry["status"] == wanted]
        return web.json_response({"robot_id": robot_id, "actions": rendered})

    @routes.get("/api/robots/{robot_id}/behavior")
    async def behavior(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        fresh = request.query.get("fresh", "").lower() in {"1", "true", "yes"}
        return web.json_response(views.behavior(runtime.behavior(robot_id), fresh=fresh))

    @routes.get("/api/robots/{robot_id}/world")
    async def world(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        return web.json_response(views.world(runtime.world.snapshot(robot_id)))

    @routes.get("/api/robots/{robot_id}/personality")
    async def personality(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        return web.json_response(views.personality(runtime.personality(robot_id)))

    @routes.get("/api/robots/{robot_id}/conversation")
    async def conversation(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        agent = await runtime.agent(robot_id)
        return web.json_response(views.conversation(agent, runtime.voice(robot_id)))

    @routes.get("/api/robots/{robot_id}/tools")
    async def tools(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        agent = await runtime.agent(robot_id)
        from robot.animation.library import EXPRESSIONS  # noqa: PLC0415 - only this route needs it

        return web.json_response(
            {
                "robot_id": robot_id,
                "tools": views.tool_catalogue(agent, TurnOrigin.USER),
                "expressions": list(EXPRESSIONS),
            }
        )

    @routes.get("/api/robots/{robot_id}/animations")
    async def animations(request: web.Request) -> web.Response:
        authorize(request)
        robot_id = await robot_id_of(request)
        engine = runtime.animations(robot_id)
        return web.json_response({"robot_id": robot_id, "animations": list(engine.library.names())})

    # -- memory --------------------------------------------------------------------------------

    @routes.get("/api/robots/{robot_id}/memory")
    async def list_memory(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
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

    @routes.get("/api/robots/{robot_id}/memory/recall")
    async def recall(request: web.Request) -> web.Response:
        """What the robot would actually put in front of a model for this question."""
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
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

    @routes.post("/api/robots/{robot_id}/memory/consolidate")
    async def consolidate(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
        result = await memory.consolidate()
        return web.json_response(
            {
                "episodes_read": result.episodes_read,
                "facts_written": result.facts_written,
                "facts": [fact.text for fact in result.facts],
            }
        )

    @routes.delete("/api/robots/{robot_id}/memory/{kind}/{memory_id}")
    async def delete_memory(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
        try:
            kind = MemoryKind(request.match_info["kind"])
        except ValueError:
            raise ApiError(400, f"unknown memory kind {request.match_info['kind']!r}") from None
        removed = await memory.forget(kind, request.match_info["memory_id"])
        if not removed:
            raise ApiError(404, "no such memory")
        return web.json_response({"deleted": True, "kind": kind.value, "id": request.match_info["memory_id"]})

    @routes.delete("/api/robots/{robot_id}/people/{person_id}")
    async def delete_person(request: web.Request) -> web.Response:
        """Delete a person and everything about them: the record, their episodes, their facts."""
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
        removed = await memory.forget_person(request.match_info["person_id"])
        return web.json_response({"deleted": removed > 0, "rows": removed})

    @routes.delete("/api/robots/{robot_id}/memory")
    async def clear_memory(request: web.Request) -> web.Response:
        authorize(request)
        memory = await memory_for(await robot_id_of(request))
        removed = await memory.clear()
        return web.json_response({"cleared": True, "rows": removed})

    # -- control -------------------------------------------------------------------------------

    @routes.post("/api/robots/{robot_id}/actions/move")
    async def move(request: web.Request) -> web.Response:
        return await submit(request, control.MoveRequest, "move")

    @routes.post("/api/robots/{robot_id}/actions/turn")
    async def turn(request: web.Request) -> web.Response:
        return await submit(request, control.TurnRequest, "turn")

    @routes.post("/api/robots/{robot_id}/actions/stop")
    async def stop(request: web.Request) -> web.Response:
        return await submit(request, control.StopRequest, "stop")

    @routes.post("/api/robots/{robot_id}/actions/head")
    async def head(request: web.Request) -> web.Response:
        return await submit(request, control.HeadRequest, "head")

    @routes.post("/api/robots/{robot_id}/actions/look_at")
    async def look_at(request: web.Request) -> web.Response:
        return await submit(request, control.LookAtRequest, "look_at")

    @routes.post("/api/robots/{robot_id}/actions/lift")
    async def lift(request: web.Request) -> web.Response:
        return await submit(request, control.LiftRequest, "lift")

    @routes.post("/api/robots/{robot_id}/expression")
    async def expression(request: web.Request) -> web.Response:
        return await submit(request, control.ExpressionRequest, "expression")

    @routes.post("/api/robots/{robot_id}/animations/{name}")
    async def play_animation(request: web.Request) -> web.Response:
        robot = await controller(request)
        payload = control.parse(control.AnimationRequest, await body_of(request))
        return web.json_response(
            await robot.animation(request.match_info["name"], payload), status=202
        )

    @routes.post("/api/robots/{robot_id}/autonomy-mode")
    async def autonomy_mode(request: web.Request) -> web.Response:
        robot = await controller(request)
        payload = control.parse(control.AutonomyRequest, await body_of(request))
        return web.json_response(await robot.set_autonomy(payload))

    @routes.post("/api/robots/{robot_id}/emergency-stop")
    async def emergency_stop(request: web.Request) -> web.Response:
        robot = await controller(request)
        payload = control.parse(control.EmergencyStopRequest, await body_of(request))
        return web.json_response(await robot.emergency_stop(payload), status=202)

    @routes.delete("/api/robots/{robot_id}/emergency-stop")
    async def clear_emergency_stop(request: web.Request) -> web.Response:
        robot = await controller(request)
        return web.json_response(await robot.clear_emergency_stop())

    @routes.delete("/api/robots/{robot_id}/actions")
    async def cancel_all(request: web.Request) -> web.Response:
        robot = await controller(request)
        return web.json_response(await robot.cancel_all())

    @routes.post("/api/robots/{robot_id}/simulate/{event}")
    async def inject(request: web.Request) -> web.Response:
        """Injection is a control operation: telling the server there is no obstacle in
        front of a robot that is about to drive is as dangerous as driving it."""
        authorize(request, Sensitivity.CONTROL)
        robot_id = await robot_id_of(request)
        name = request.match_info["event"]
        entry = simulate.INJECTIONS.get(name)
        if entry is None:
            raise ApiError(400, f"unknown injection {name!r}; known: {', '.join(sorted(simulate.INJECTIONS))}")
        model, handler = entry
        payload = control.parse(model, await body_of(request))
        return web.json_response(await handler(runtime, robot_id, payload), status=202)

    # -- events --------------------------------------------------------------------------------

    @routes.get("/api/events")
    async def event_stream(request: web.Request) -> web.StreamResponse:
        authorize(request)
        try:
            categories = events.parse_categories(request.query.get("categories"))
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        stream = events.EventStream(robot_id=request.query.get("robot"), categories=categories)
        served: web.StreamResponse = await events.serve_stream(request, runtime.events, stream)
        return served

    # -- wiring --------------------------------------------------------------------------------

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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("robot management API: %s %s failed", request.method, request.path)
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    app = web.Application(middlewares=[errors])
    app.add_routes(routes)
    # An attribute rather than an app key: aiohttp warns about untyped string keys, and a
    # typed AppKey would be a module-scope aiohttp import in a file that must not have one.
    app.nilo_security = guard
    return app


async def run_api(
    runtime: Any,
    memories: MemorySource,
    *,
    token: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_API_PORT,
    require_auth: bool = True,
    allow_remote_control: bool | None = None,
) -> Any:
    """Start the management API and return its runner. The caller owns shutdown.

    Binds to loopback by default. A wider binding is allowed — reading telemetry from a
    laptop is a reasonable thing to want — but control endpoints stay refused on it unless
    ``NILO_ROBOT_API_ALLOW_REMOTE_CONTROL`` is set, and the binding is logged loudly.
    """
    from aiohttp import web  # noqa: PLC0415

    security = ApiSecurity.from_env(host=host, require_auth=require_auth)
    if token is not None:
        security.token = token
    if allow_remote_control is not None:
        security.allow_remote_control = allow_remote_control
    security.warn_if_open()

    app = build_app(runtime, memories, security=security)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(
        "robot management API listening on http://%s:%d (control %s)",
        host,
        port,
        "enabled" if security.control_enabled else "refused: not loopback",
    )
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
            logger.error("robot management API task failed: %s", error)
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
    "ALLOW_REMOTE_CONTROL_ENV",
    "API_HOST_ENV",
    "API_PORT_ENV",
    "DEFAULT_API_PORT",
    "ApiError",
    "ApiSecurity",
    "MemorySource",
    "admin_token_from_env",
    "build_app",
    "is_loopback",
    "run_api",
    "supervise",
]
