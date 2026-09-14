"""Domain objects as JSON, in one place.

Every read endpoint in the management API renders through a function here, so "what does
a robot look like over HTTP" has one answer rather than one per route. The dashboard reads
exactly these shapes and nothing else.

Two rules:

* **Current values, never series.** A telemetry view is a snapshot. The event stream is
  where change lives (:mod:`robot.api.events`); an endpoint that returned history would
  grow without bound and be wrong about "now" by the time it was read.
* **Defensive.** Every field is optional somewhere: a robot that has never reported its
  battery has no battery, and a view that raises on that turns a dashboard panel into a
  500. Missing is ``None``, not an exception.
"""

from __future__ import annotations

from typing import Any

from robot.state.models import RobotState
from robot.state.world import WorldState


def robot_summary(state: RobotState | None, robot_id: str) -> dict[str, Any]:
    """The list entry: who this robot is and whether it is here."""
    if state is None:
        return {"robot_id": robot_id, "connected": False}
    identity = state.identity
    connection = state.connection
    return {
        "robot_id": state.robot_id,
        "name": identity.name,
        "device_id": identity.device_id,
        "hardware_model": identity.hardware_model,
        "firmware_version": identity.firmware_version,
        "connected": state.is_connected,
        "status": connection.status.value,
        "session_id": connection.session_id,
        "remote_address": connection.remote_address,
        "connected_at": connection.connected_at.isoformat(),
        "last_seen_at": connection.last_seen_at.isoformat(),
        "reconnect_count": connection.reconnect_count,
    }


def robot_state(state: RobotState | None) -> dict[str, Any]:
    """Telemetry: pose, motion, battery, sensors, audio, vision, expression, activity."""
    if state is None:
        return {}
    telemetry = state.telemetry
    return {
        "updated_at": telemetry.updated_at.isoformat(),
        "pose": _model(telemetry.pose),
        "motion": _model(telemetry.motion),
        "battery": _model(telemetry.battery),
        "sensors": _model(telemetry.sensors),
        "audio": _model(telemetry.audio),
        "vision": _model(telemetry.vision),
        "expression": _model(telemetry.expression),
        "activity": _model(telemetry.activity),
    }


def capabilities(state: RobotState | None) -> dict[str, Any]:
    """What the device said it can do, as discovered — never hardcoded."""
    if state is None:
        return {"mcp": False, "tools": []}
    found = state.capabilities
    return {
        "mcp": found.mcp,
        "features": sorted(found.features),
        "protocol_version": found.protocol_version,
        "server_name": found.server_name,
        "server_version": found.server_version,
        "malformed_tools": found.malformed_tools,
        "tools": [
            {
                "name": tool.name,
                "raw_name": tool.raw_name,
                "description": tool.description,
                "required": list(tool.required_arguments),
            }
            for tool in found.tools
        ],
    }


def action(record: Any) -> dict[str, Any]:
    """One action's whole life, as the executor recorded it."""
    return {
        "action_id": record.action_id,
        "robot_id": record.robot_id,
        "type": record.action_type.value,
        "source": record.source.value,
        "priority": int(record.priority),
        "status": record.status.value,
        "parameters": dict(record.parameters),
        "resources": [resource.value for resource in record.resources],
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "device_action_id": record.device_action_id,
        "result": record.result,
        "error": None
        if record.error is None
        else {
            "code": record.error.code,
            "message": record.error.message,
            "reason": record.error.reason.value if record.error.reason else None,
        },
    }


def world(snapshot: WorldState) -> dict[str, Any]:
    """What the robot believes is around it, with ages rather than timestamps."""
    now = snapshot.updated_at
    return {
        "robot_id": snapshot.robot_id,
        "updated_at": now.isoformat(),
        "environment": _model(snapshot.environment),
        "attention": snapshot.attention_target,
        "entities": [
            {
                "id": entity.id,
                "type": entity.type.value,
                "confidence": round(entity.confidence, 3),
                "age_s": round(entity.age_s(now), 2),
                "lifetime_s": round(entity.lifetime_s(now), 2),
                "attributes": dict(entity.attributes),
                "position": _model(entity.position),
                "image_point": _model(entity.image_point),
            }
            for entity in sorted(snapshot.entities.values(), key=lambda item: item.last_seen, reverse=True)
        ],
        "interaction": _model(snapshot.current_interaction),
        "blocked": snapshot.blocked,
        "moving": snapshot.moving,
        "battery_percent": snapshot.battery_percent,
        "charging": snapshot.charging,
        "touched": snapshot.touched,
    }


def personality(model: Any) -> dict[str, Any]:
    """Traits and the internal control variables. Never called emotions."""
    if model is None:
        return {}
    traits = getattr(model, "traits", None)
    return {
        "traits": _model(traits),
        "drives": {
            name: round(float(getattr(model, name, 0.0)), 3)
            for name in ("curiosity", "boredom", "social_need", "energy", "valence", "arousal", "confidence")
        },
    }


def conversation(agent: Any, loop: Any) -> dict[str, Any]:
    """What the robot is saying and to whom, plus the audio state it is in."""
    if agent is None:
        return {"available": False, "audio_state": "idle", "messages": []}
    talk = agent.conversation
    return {
        "available": agent.available,
        "audio_state": getattr(getattr(loop, "state", None), "value", "idle"),
        "person_id": talk.person_id,
        "turns": talk.turns,
        "active_turn": talk.active_turn,
        "messages": [
            {"role": message.get("role", ""), "content": str(message.get("content", ""))[:500]}
            for message in talk.messages[-12:]
        ],
        "tools": [spec.name for spec in agent.toolkit.specs()],
    }


def tool_catalogue(agent: Any, origin: Any) -> list[dict[str, Any]]:
    """Every robot tool, with whether it is usable right now and why not."""
    if agent is None:
        return []
    toolkit = agent.toolkit
    catalogue = []
    for spec in toolkit.specs():
        decision = toolkit.authorize(spec, origin=origin)
        catalogue.append(
            {
                "name": spec.name,
                "qualified_name": spec.qualified_name,
                "permission": spec.permission.value,
                "description": spec.description,
                "available": decision.allowed,
                "refused_because": decision.reason,
            }
        )
    return catalogue


def behavior(engine: Any, *, fresh: bool = False) -> dict[str, Any]:
    """The last decision, with every candidate's score. Why the robot is doing this."""
    if engine is None:
        return {"available": False}
    data: dict[str, Any] = dict(engine.explain_data(fresh=fresh))
    data["available"] = True
    data["mode"] = engine.mode.value
    data["running"] = getattr(engine.scheduler, "running", None)
    return data


def _model(value: Any) -> dict[str, Any] | None:
    """A pydantic model as plain JSON, or ``None``. Datetimes become ISO strings."""
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if dump is None:
        return None
    result: dict[str, Any] = dump(mode="json")
    return result


__all__ = [
    "action",
    "behavior",
    "capabilities",
    "conversation",
    "personality",
    "robot_state",
    "robot_summary",
    "tool_catalogue",
    "world",
]
