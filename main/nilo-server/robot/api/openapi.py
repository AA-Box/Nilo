"""The OpenAPI document, built from the request models rather than written beside them.

    GET /api/openapi.json

Every control endpoint's request body is a pydantic model in :mod:`robot.api.control` and
:mod:`robot.api.simulate`, and its schema here comes from ``model_json_schema()``. A
hand-written document is a document that is wrong the first time somebody adds a field; a
test asserts that every route the application registers appears here, so a new endpoint
cannot be undocumented either.

The document is deliberately honest about the two things a generated one usually is not:
which endpoints are refused when the API is not on loopback, and that a ``202`` means the
robot *accepted* a command rather than finished it.
"""

from __future__ import annotations

from typing import Any

from robot.api.control import (
    AnimationRequest,
    AutonomyRequest,
    EmergencyStopRequest,
    ExpressionRequest,
    HeadRequest,
    LiftRequest,
    LookAtRequest,
    MoveRequest,
    StopRequest,
    TurnRequest,
)
from robot.api.events import ALL_CATEGORIES
from robot.api.simulate import INJECTIONS
from robot import __version__

#: Reusable parameter: the robot a path applies to.
_ROBOT_PARAM = {
    "name": "robot_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "The robot's id, as `GET /api/robots` lists it.",
}

_CONTROL_RESPONSES = {
    "202": {"description": "Accepted. The robot has the command; it has not finished it."},
    "400": {"description": "The body did not validate."},
    "401": {"description": "Missing or wrong admin token."},
    "403": {"description": "Control endpoints are disabled on this binding."},
    "404": {"description": "No such robot."},
    "409": {"description": "The safety policy refused it. The body carries the typed reason."},
    "429": {"description": "Rate limited."},
}

_READ_RESPONSES = {
    "200": {"description": "OK."},
    "401": {"description": "Missing or wrong admin token."},
    "404": {"description": "No such robot."},
}


def _body(model: type[Any]) -> dict[str, Any]:
    return {
        "required": False,
        "content": {"application/json": {"schema": _schema(model)}},
    }


def _schema(model: type[Any]) -> dict[str, Any]:
    schema: dict[str, Any] = model.model_json_schema()
    schema.pop("$defs", None)
    schema["additionalProperties"] = False
    return schema


def _get(summary: str, *, tag: str, robot: bool = True, params: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": summary,
        "tags": [tag],
        "responses": dict(_READ_RESPONSES),
        "security": [{"bearerAuth": []}],
    }
    parameters = ([_ROBOT_PARAM] if robot else []) + list(params or [])
    if parameters:
        operation["parameters"] = parameters
    return {"get": operation}


def _post(summary: str, model: type[Any] | None, *, tag: str, params: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": summary,
        "tags": [tag],
        "responses": dict(_CONTROL_RESPONSES),
        "security": [{"bearerAuth": []}],
        "parameters": [_ROBOT_PARAM] + list(params or []),
    }
    if model is not None:
        operation["requestBody"] = _body(model)
    return {"post": operation}


def document(security: Any = None) -> dict[str, Any]:
    """The whole document. ``security`` fills in what this binding actually allows."""
    described = security.describe() if security is not None else {}
    name_param = {
        "name": "name",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": "The animation's name, as `GET /api/robots/{robot_id}/animations` lists it.",
    }
    event_param = {
        "name": "event",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": sorted(INJECTIONS)},
        "description": "What to inject.",
    }

    paths: dict[str, Any] = {
        "/health": {
            "get": {
                "summary": "Liveness. The only unauthenticated endpoint, and it says nothing about any robot.",
                "tags": ["system"],
                "responses": {"200": {"description": "OK."}},
                "security": [],
            }
        },
        "/api/meta": {
            "get": {
                "summary": "What this binding allows: host, control, authentication, rate limit.",
                "tags": ["system"],
                "responses": {"200": {"description": "OK."}},
                "security": [],
            }
        },
        "/api/openapi.json": {
            "get": {
                "summary": "This document.",
                "tags": ["system"],
                "responses": {"200": {"description": "OK."}},
                "security": [],
            }
        },
        "/api/robots": _get("Every robot this server knows about.", tag="read", robot=False),
        "/api/robots/{robot_id}": _get("Identity, connection and memory counts.", tag="read"),
        "/api/robots/{robot_id}/state": _get("Current telemetry. A snapshot, never a series.", tag="read"),
        "/api/robots/{robot_id}/capabilities": _get("What the device published, as discovered.", tag="read"),
        "/api/robots/{robot_id}/actions": _get(
            "Actions, newest first.",
            tag="read",
            params=[
                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                {"name": "status", "in": "query", "schema": {"type": "string"}},
            ],
        ),
        "/api/robots/{robot_id}/behavior": _get(
            "The behaviour engine's last decision, with every candidate's score.",
            tag="read",
            params=[{"name": "fresh", "in": "query", "schema": {"type": "boolean", "default": False}}],
        ),
        "/api/robots/{robot_id}/world": _get("What the robot believes is around it.", tag="read"),
        "/api/robots/{robot_id}/memory": _get(
            "Working, episodic, semantic and person memory.",
            tag="read",
            params=[
                {"name": "kind", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                {"name": "person", "in": "query", "schema": {"type": "string"}},
            ],
        ),
        "/api/robots/{robot_id}/memory/recall": _get(
            "What would actually go in front of a model for this question.",
            tag="read",
            params=[
                {"name": "q", "in": "query", "schema": {"type": "string"}},
                {"name": "person", "in": "query", "schema": {"type": "string"}},
            ],
        ),
        "/api/robots/{robot_id}/personality": _get(
            "Traits and the internal control variables.", tag="read"
        ),
        "/api/robots/{robot_id}/conversation": _get(
            "The transcript, the audio state and the person speaking.", tag="read"
        ),
        "/api/robots/{robot_id}/tools": _get(
            "Every robot tool, and whether it is usable right now.", tag="read"
        ),
        "/api/robots/{robot_id}/animations": _get("The animations this robot can play.", tag="read"),
        "/api/events": _get(
            "Server-sent events. Everything the robot subsystem publishes.",
            tag="events",
            robot=False,
            params=[
                {"name": "robot", "in": "query", "schema": {"type": "string"}},
                {
                    "name": "categories",
                    "in": "query",
                    "schema": {"type": "string"},
                    "description": "Comma-separated. One or more of: " + ", ".join(sorted(ALL_CATEGORIES)),
                },
            ],
        ),
        "/api/robots/{robot_id}/actions/move": _post("Drive straight.", MoveRequest, tag="control"),
        "/api/robots/{robot_id}/actions/turn": _post("Turn in place.", TurnRequest, tag="control"),
        "/api/robots/{robot_id}/actions/stop": _post(
            "Stop. Never refused by the permission policy.", StopRequest, tag="control"
        ),
        "/api/robots/{robot_id}/actions/head": _post("Point the head.", HeadRequest, tag="control"),
        "/api/robots/{robot_id}/actions/look_at": _post(
            "Point the head at a spot in the camera frame.", LookAtRequest, tag="control"
        ),
        "/api/robots/{robot_id}/actions/lift": _post("Raise or lower the lift.", LiftRequest, tag="control"),
        "/api/robots/{robot_id}/expression": _post(
            "Show an expression.", ExpressionRequest, tag="control"
        ),
        "/api/robots/{robot_id}/animations/{name}": _post(
            "Play a named animation.", AnimationRequest, tag="control", params=[name_param]
        ),
        "/api/robots/{robot_id}/autonomy-mode": _post(
            "Set how much the robot may start on its own.", AutonomyRequest, tag="control"
        ),
        "/api/robots/{robot_id}/emergency-stop": _post(
            "Latch the robot. A request, not a guarantee: the firmware watchdog is the guarantee.",
            EmergencyStopRequest,
            tag="control",
        ),
        "/api/robots/{robot_id}/simulate/{event}": _post(
            "Inject a perception event, through the same doors real perception uses.",
            None,
            tag="simulate",
            params=[event_param],
        ),
    }

    # The delete paths, which are the other half of the memory story.
    for path, summary in (
        ("/api/robots/{robot_id}/memory", "Delete everything this robot remembers."),
        ("/api/robots/{robot_id}/memory/{kind}/{memory_id}", "Delete one memory."),
        ("/api/robots/{robot_id}/people/{person_id}", "Delete a person and everything about them."),
    ):
        paths.setdefault(path, {})["delete"] = {
            "summary": summary,
            "tags": ["read"],
            "responses": dict(_READ_RESPONSES),
            "security": [{"bearerAuth": []}],
            "parameters": [_ROBOT_PARAM],
        }
    paths["/api/robots/{robot_id}/memory/consolidate"] = {
        "post": {
            "summary": "Fold episodes into semantic facts now.",
            "tags": ["read"],
            "responses": dict(_READ_RESPONSES),
            "security": [{"bearerAuth": []}],
            "parameters": [_ROBOT_PARAM],
        }
    }
    paths["/api/robots/{robot_id}/emergency-stop"]["delete"] = {
        "summary": "Clear the emergency-stop latch.",
        "tags": ["control"],
        "responses": dict(_CONTROL_RESPONSES),
        "security": [{"bearerAuth": []}],
        "parameters": [_ROBOT_PARAM],
    }
    paths["/api/robots/{robot_id}/actions"]["delete"] = {
        "summary": "Cancel everything pending and running for this robot.",
        "tags": ["control"],
        "responses": dict(_CONTROL_RESPONSES),
        "security": [{"bearerAuth": []}],
        "parameters": [_ROBOT_PARAM],
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Nilo robot management API",
            "version": __version__,
            "description": (
                "Inspect and drive a robot. Every control endpoint submits a typed action to "
                "the same executor and the same safety policy as a behaviour's own command; "
                "there is no privileged path. A 202 means the robot accepted the command, not "
                "that it finished it, and a stop the backend sends is a request — the firmware "
                "watchdog is the guarantee.\n\n"
                f"This binding: {described or 'unknown'}."
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "read", "description": "Inspection. Token required."},
            {"name": "control", "description": "Moves a robot. Loopback-only unless explicitly allowed."},
            {"name": "events", "description": "The live stream."},
            {"name": "simulate", "description": "Injected perception. Treated as control."},
            {"name": "system", "description": "Liveness."},
        ],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "description": "The admin token."}
            }
        },
        "paths": paths,
    }


__all__ = ["document"]
