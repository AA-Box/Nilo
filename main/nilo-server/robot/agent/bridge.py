"""The seam between the robot toolkit and the inherited tool system.

    from robot.agent.bridge import register_robot_tools
    register_robot_tools()          # idempotent; safe to call at import time

The inherited server has one flat tool namespace and five executors behind it
(``core/providers/tools/unified_tool_handler.py``). Robot tools join it as server plugins
of type ``IOT_CTL``, which is the only type auto-exposed to the model with no
``config.yaml`` edit (``ServerPluginExecutor.get_tools``) and one of the two that are
handed the connection.

Three things this module exists to get right:

* **``core`` and ``plugins`` are imported inside functions.** ``config/logger.py`` imports
  ``robot.__version__``, so an eager import here would be a cycle every logging process
  pays for (docs/robot-architecture.md R12, ``tests/robot/test_layering.py``).
* **Collision detection.** The namespace is flat and executors are registered
  server-plugins-first, device-MCP-last, so a device that publishes its own ``robot_move``
  silently replaces the guarded one with nothing worse than a logged warning
  (``ToolManager.get_all_tools``). :func:`detect_collisions` finds that at session start
  and says so loudly.
* **Handlers return immediately.** The chat loop awaits tool futures sequentially on a
  five-worker pool with no cancellation, each bounded by ``tool_call_timeout``
  (``core/connection.py``). Every handler here submits and returns; none waits for a robot
  to finish moving.

The handlers are thin on purpose: they resolve the robot from the connection, hand the
call to :class:`~robot.agent.tools.RobotToolkit`, and translate the outcome into the
inherited ``ActionResponse`` vocabulary. All the judgement is one layer down.
"""

from __future__ import annotations

import logging
from typing import Any

from robot.agent.permissions import ToolPermission, TurnOrigin
from robot.agent.tools import TOOL_NAMES, TOOL_SPECS, RobotToolkit, RobotToolSpec
from robot.session import ROBOT_ATTR

logger = logging.getLogger(__name__)

#: Set once :func:`register_robot_tools` has run. Registration is global state in the
#: inherited registry, and registering twice would overwrite rather than duplicate — but
#: it would also log fourteen misleading lines every time a connection is made.
_registered = False


def registered() -> bool:
    return _registered


def register_robot_tools(*, force: bool = False) -> tuple[str, ...]:
    """Register every robot tool as an ``IOT_CTL`` server plugin. Returns the names.

    Idempotent. Imports ``plugins.register`` lazily, which is why this is a function and
    not fourteen decorators at module scope.
    """
    global _registered
    if _registered and not force:
        return TOOL_NAMES
    from plugins.register import ToolType, register_function  # noqa: PLC0415 - deliberately lazy

    for spec in TOOL_SPECS:
        register_function(spec.name, _description(spec), ToolType.IOT_CTL)(_make_handler(spec))
    _registered = True
    logger.info("registered %d robot tools with the inherited tool system", len(TOOL_SPECS))
    return TOOL_NAMES


def detect_collisions(capabilities: Any) -> tuple[str, ...]:
    """Device tool names that would shadow a guarded robot tool. Empty is the good case.

    Called once per session, after capability discovery. The inherited tool manager only
    logs a warning when two executors publish the same name, and the device executor is
    registered last, so a collision means the model's ``robot_move`` stops being the
    guarded one — which is exactly the failure the whole action layer exists to prevent.
    """
    names = getattr(capabilities, "tool_names", ()) or ()
    clashing = tuple(sorted(set(names) & set(TOOL_NAMES)))
    if clashing:
        logger.error(
            "device tools collide with guarded robot tools: %s. The device tool would "
            "shadow the safety-checked one in the flat tool namespace; rename the "
            "firmware tools or do not expose them to the model.",
            ", ".join(clashing),
        )
    return clashing


# -- handler construction ------------------------------------------------------------------------


def _description(spec: RobotToolSpec) -> dict[str, Any]:
    """The static description the inherited registry holds.

    Static because the registry is process-wide and a description is rendered once and
    cached by the intent provider (``core/providers/intent/intent_llm``), so it cannot
    depend on one robot's limits. The per-robot schema, with live bounds, is what
    :meth:`RobotToolkit.function_descriptions` produces for the agent.
    """
    return spec.schema()


def _make_handler(spec: RobotToolSpec) -> Any:
    """One ``async def handler(conn, **arguments)`` for one tool."""

    async def handler(conn: Any, **arguments: Any) -> Any:
        return await _dispatch(conn, spec, arguments)

    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    handler.__module__ = "robot_tools"  # the module key the plugin registry groups under
    return handler


async def _dispatch(conn: Any, spec: RobotToolSpec, arguments: dict[str, Any]) -> Any:
    from plugins.register import Action, ActionResponse  # noqa: PLC0415 - deliberately lazy

    session = getattr(conn, ROBOT_ATTR, None)
    robot_id = getattr(session, "robot_id", None)
    if not robot_id:
        return ActionResponse(
            action=Action.ERROR, response="This session is not attached to a robot."
        )
    toolkit = _toolkit_for(conn, robot_id)
    outcome = await toolkit.call(
        spec.name,
        arguments,
        origin=TurnOrigin.USER,
        person_id=getattr(conn, "current_speaker", None),
    )
    if not outcome.ok:
        # The model explains the refusal: it has the conversation, and "I cannot move,
        # there is a drop in front of me" is not a sentence this layer should be writing.
        return ActionResponse(action=Action.REQLLM, result=outcome.as_text())
    if spec.permission in (ToolPermission.MOTION, ToolPermission.EXPRESSIVE):
        # Acting tools get a terse confirmation written into history rather than a second
        # model round trip: the robot has already started doing the thing.
        return ActionResponse(
            action=Action.RECORD, result=outcome.as_text(), response=_confirmation(spec)
        )
    return ActionResponse(action=Action.REQLLM, result=outcome.as_text())


def _toolkit_for(conn: Any, robot_id: str) -> RobotToolkit:
    """The toolkit for this connection, built once and kept on the connection.

    On the connection rather than in a module dictionary: the inherited handler owns the
    lifetime, two sessions for one device are normal during a reconnect, and a
    process-wide map keyed by robot id is a leak with a race in it.
    """
    from robot.runtime import get_runtime  # noqa: PLC0415 - avoids importing the runtime to log

    cached = getattr(conn, "nilo_robot_toolkit", None)
    if isinstance(cached, RobotToolkit) and cached.robot_id == robot_id:
        return cached
    runtime = get_runtime()
    toolkit = RobotToolkit(runtime, robot_id, animations=runtime.animations(robot_id))
    conn.nilo_robot_toolkit = toolkit
    return toolkit


def _confirmation(spec: RobotToolSpec) -> str:
    return _CONFIRMATIONS.get(spec.name, "Okay.")


#: What the robot says when an acting tool is accepted and no second model call is made.
#: Short on purpose: the point is that the robot acknowledged, not that it narrated.
_CONFIRMATIONS: dict[str, str] = {
    "robot_move": "Okay.",
    "robot_turn": "Okay.",
    "robot_stop": "Stopping.",
    "robot_look_at": "Okay.",
    "robot_follow_person": "Following you.",
    "robot_stop_following": "Okay, I will stay here.",
    "robot_play_animation": "Okay.",
    "robot_set_expression": "Okay.",
}


__all__ = ["detect_collisions", "register_robot_tools", "registered"]
