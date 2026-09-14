"""The live event stream: everything the robot subsystem publishes, as server-sent events.

    GET /api/events                       every event, every robot
    GET /api/events?robot=nilo-sim-01     one robot
    GET /api/events?categories=action,world

Server-sent events rather than a WebSocket, for three reasons that all point the same way:
the traffic is one-directional (the control plane is the REST API), SSE reconnects on its
own, and it is a plain HTTP response — so it works through the same auth header, the same
middleware and the same test client as every other endpoint here. A WebSocket would need a
second auth path, and the token would end up in a query string.

Categories are the subscription unit, because "show me actions" is what an operator asks
and ``ActionSubmitted | ActionStarted | ActionFinished`` is what they mean:

``telemetry``     pose, battery, sensors, the composite update
``action``        submitted, started, finished
``behavior``      evaluated, selected, started, completed, interrupted
``world``         people, objects, faces appearing and going away
``conversation``  utterances, turns, speech, audio state
``error``         anything that failed: a tool, a motion, a turn
``system``        connections, capabilities, the emergency stop

The stream is **bounded and lossy on purpose**. Each subscriber has its own queue and a
slow reader loses its oldest events rather than delaying the publisher — the same rule the
event bus itself follows (``robot/events/bus.py``). A dropped telemetry frame is not worth
stalling a robot for.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

from robot.events.types import RobotEvent

logger = logging.getLogger(__name__)

#: How many events one stream may fall behind before it starts losing the oldest.
STREAM_QUEUE_SIZE = 256

#: How often a comment frame is sent when nothing is happening, in seconds. Keeps proxies
#: and load balancers from closing an idle stream.
KEEPALIVE_S = 15.0

#: Event class name -> category. A class that is not here lands in ``system``, so a new
#: event type shows up in the stream on the day it is added rather than the day somebody
#: remembers to add it here.
CATEGORIES: dict[str, str] = {
    "TelemetryUpdated": "telemetry",
    "BatteryUpdated": "telemetry",
    "PoseUpdated": "telemetry",
    "SensorUpdated": "telemetry",
    "ActionSubmitted": "action",
    "ActionStarted": "action",
    "ActionFinished": "action",
    "MotionCompleted": "action",
    "MotionFailed": "error",
    "ToolCallStarted": "action",
    "ToolCallCompleted": "action",
    "ToolCallFailed": "error",
    "ToolCallRefused": "error",
    "BehaviorEvaluated": "behavior",
    "BehaviorSelected": "behavior",
    "BehaviorStarted": "behavior",
    "BehaviorCompleted": "behavior",
    "BehaviorInterrupted": "behavior",
    "AnimationStarted": "behavior",
    "AnimationFinished": "behavior",
    "AnimationCancelled": "behavior",
    "PersonDetected": "world",
    "PersonLost": "world",
    "FaceDetected": "world",
    "KnownPersonRecognized": "world",
    "UnknownPersonDetected": "world",
    "ObjectDetected": "world",
    "ObjectLost": "world",
    "VisionFrameProcessed": "world",
    "AgentTurnStarted": "conversation",
    "AgentTurnCompleted": "conversation",
    "AgentTurnFailed": "error",
    "UtteranceRecognized": "conversation",
    "AudioStateChanged": "conversation",
    "SpeechRequested": "conversation",
    "SpeechStarted": "conversation",
    "SpeechFinished": "conversation",
    "SpeechInterrupted": "conversation",
    "RobotConnected": "system",
    "RobotDisconnected": "system",
    "CapabilitiesRefreshed": "system",
    "ToolDiscovered": "system",
    "EmergencyStopChanged": "system",
}

#: Every category a client may ask for.
ALL_CATEGORIES: frozenset[str] = frozenset(CATEGORIES.values()) | {"system"}


def category_of(event: RobotEvent) -> str:
    """Which category one event belongs to. Unknown types are ``system``, never dropped."""
    return CATEGORIES.get(type(event).__name__, "system")


def encode(event: RobotEvent) -> str:
    """One event as an SSE frame: an event name, an id, and a JSON payload."""
    payload = event.model_dump(mode="json")
    payload["event"] = type(event).__name__
    payload["category"] = category_of(event)
    body = json.dumps(payload, default=str)
    return f"id: {event.event_id}\nevent: {category_of(event)}\ndata: {body}\n\n"


class EventStream:
    """One subscriber's view of the bus: filtered, bounded, and lossy when it falls behind."""

    def __init__(
        self,
        *,
        robot_id: str | None = None,
        categories: Iterable[str] | None = None,
        queue_size: int = STREAM_QUEUE_SIZE,
    ) -> None:
        self.robot_id = robot_id
        self.categories = frozenset(categories) if categories else ALL_CATEGORIES
        self.queue: asyncio.Queue[RobotEvent] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0

    def wants(self, event: RobotEvent) -> bool:
        if self.robot_id is not None and event.robot_id != self.robot_id:
            return False
        return category_of(event) in self.categories

    def offer(self, event: RobotEvent) -> None:
        """Queue an event, dropping the oldest when the reader has fallen behind."""
        if not self.wants(event):
            return
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                    pass
                self.dropped += 1


def parse_categories(raw: str | None) -> frozenset[str] | None:
    """``"action,world"`` -> the set. ``None`` or empty means everything.

    An unknown name is an error rather than a silent empty stream: a dashboard that asked
    for ``behaviour`` and got nothing would look like a broken robot.
    """
    if not raw:
        return None
    wanted = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = wanted - ALL_CATEGORIES
    if unknown:
        raise ValueError(
            f"unknown event categories: {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(ALL_CATEGORIES))}"
        )
    return wanted or None


async def serve_stream(request: Any, bus: Any, stream: EventStream) -> Any:
    """Attach ``stream`` to ``bus`` and write frames until the client goes away."""
    from aiohttp import web  # noqa: PLC0415 - deliberately lazy, like the rest of robot/api

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Proxies that buffer a stream turn a live event feed into a batch job.
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    subscription = bus.subscribe(RobotEvent, stream.offer)
    try:
        await response.write(b": connected\n\n")
        while True:
            try:
                event = await asyncio.wait_for(stream.queue.get(), timeout=KEEPALIVE_S)
            except (TimeoutError, asyncio.TimeoutError):
                await response.write(b": keepalive\n\n")
                continue
            await response.write(encode(event).encode("utf-8"))
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception as exc:  # a broken pipe is the normal way a stream ends
        logger.debug("robot event stream closed: %s", exc)
    finally:
        bus.unsubscribe(subscription)
        if stream.dropped:
            logger.info("robot event stream fell behind and dropped %d events", stream.dropped)
    return response


__all__ = [
    "ALL_CATEGORIES",
    "CATEGORIES",
    "KEEPALIVE_S",
    "STREAM_QUEUE_SIZE",
    "EventStream",
    "category_of",
    "encode",
    "parse_categories",
    "serve_stream",
]
