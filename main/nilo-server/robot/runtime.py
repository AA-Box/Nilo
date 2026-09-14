"""The robot control plane: one object that owns the subsystem for a process.

It wires the pieces together and owns the connection lifecycle:

    attach(device)  ->  register  ->  discover capabilities (off the read loop)
    detach(device)  ->  unregister, cancel discovery, close the tool channel

Nothing here is a singleton by construction: :class:`RobotRuntime` is built per process
by :func:`get_runtime` and per test by calling it directly, so two tests never share a
registry, a bus or a state store.

Discovery deliberately runs in its own task. The inherited read loop awaits every message
handler inline (robot-architecture R3), so anything that waits on a device would stall
ingestion of all later frames, audio included.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from robot.devices.mcp import McpError, McpUnsupportedError
from robot.devices.registry import RobotRegistry
from robot.devices.tools import RobotCapabilityRegistry, ToolChannel
from robot.events.bus import EventBus
from robot.events.types import ToolCallCompleted, ToolCallFailed, ToolCallStarted
from robot.state.models import (
    DeviceInfo,
    DisconnectReason,
    RobotCapabilities,
    RobotState,
    RobotTelemetry,
)
from robot.state.store import InMemoryRobotStateStore, RobotStateStore
from robot.state.world_model import WorldModel

if TYPE_CHECKING:  # pragma: no cover - imported lazily below to keep the graph acyclic
    from robot.actions.executor import RobotActionExecutor
    from robot.animation.engine import AnimationEngine
    from robot.behavior.base import AutonomyMode
    from robot.behavior.engine import BehaviorEngine
    from robot.personality.model import PersonalityModel
    from robot.vision.pipeline import VisionPipeline

logger = logging.getLogger(__name__)

#: How long capability discovery may take before it is abandoned, in seconds.
DEFAULT_DISCOVERY_TIMEOUT = 10.0


class UnknownRobotError(LookupError):
    """An operation named a robot that is not registered."""


class RobotRuntime:
    """Everything the robot subsystem needs at runtime, in one place."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        store: RobotStateStore | None = None,
        capabilities: RobotCapabilityRegistry | None = None,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        personality_store: Any = None,
    ) -> None:
        self._events = events or EventBus()
        self._store = store or InMemoryRobotStateStore()
        self._capabilities = capabilities or RobotCapabilityRegistry()
        self._registry = RobotRegistry(self._store, self._events, self._capabilities)
        self._channels: dict[str, ToolChannel] = {}
        self._discovery: dict[str, asyncio.Task[None]] = {}
        self._discovery_timeout = discovery_timeout
        self._actions: RobotActionExecutor | None = None
        self._world = WorldModel()
        self._behaviors: dict[str, BehaviorEngine] = {}
        self._personalities: dict[str, PersonalityModel] = {}
        # Opt-in: a runtime with no store keeps personalities in memory. Persistence is a
        # deployment decision, and a test (or a CLI) must not write into `data/` just by
        # constructing a runtime.
        self._personality_store = personality_store
        self._animations: dict[str, AnimationEngine] = {}
        self._vision: dict[str, VisionPipeline] = {}
        self._autonomy: AutonomyMode | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def registry(self) -> RobotRegistry:
        return self._registry

    @property
    def states(self) -> RobotStateStore:
        return self._store

    @property
    def capabilities(self) -> RobotCapabilityRegistry:
        return self._capabilities

    @property
    def actions(self) -> RobotActionExecutor:
        """The action executor for this runtime, created on first use.

        Imported inside the property on purpose: ``robot/actions`` imports the runtime's
        protocol, so a module-scope import here would be a cycle
        (docs/robot-architecture.md Sect. 7). It also keeps ``import robot.runtime``
        cheap for the processes that only want the registry.
        """
        from robot.actions.executor import RobotActionExecutor

        if self._actions is None:
            self._actions = RobotActionExecutor(self)
        return self._actions

    @property
    def world(self) -> WorldModel:
        """The world model: what the robot believes is around it, folded from events."""
        return self._world

    @property
    def autonomy(self) -> AutonomyMode:
        """The autonomy mode new behaviour engines start in. ``NORMAL`` until set.

        The import is local for the same reason as the one in :meth:`behavior`: importing
        the behaviour package eagerly would make every process that logs pay for it
        (``config/logger.py`` imports ``robot.__version__``).
        """
        from robot.behavior.base import AutonomyMode

        if self._autonomy is None:
            self._autonomy = AutonomyMode.NORMAL
        return self._autonomy

    async def set_autonomy(self, mode: AutonomyMode) -> None:
        """Change the autonomy mode of every robot, now and for the ones that connect later."""
        self._autonomy = mode
        for engine in list(self._behaviors.values()):
            await engine.set_mode(mode)

    def robot(self, robot_id: str) -> Any:
        """A semantic handle: ``await runtime.robot(id).move(distance_mm=300)``."""
        return self.actions.robot(robot_id)

    def personality(self, robot_id: str) -> PersonalityModel:
        """The personality and internal control variables for one robot, created on first use.

        Attached to the bus immediately: the control variables are moved by what happens,
        and a personality that is not listening is a personality that never changes.
        """
        from robot.personality.model import PersonalityModel
        from robot.personality.traits import load_traits

        model = self._personalities.get(robot_id)
        if model is None:
            model = PersonalityModel(robot_id, traits=load_traits(), store=self._personality_store)
            model.attach(self._events)
            self._personalities[robot_id] = model
        return model

    def animations(self, robot_id: str) -> AnimationEngine:
        """The animation engine for one robot, created on first use.

        Commands go through the same semantic handle as everything else (attributed to
        :attr:`~robot.state.actions.ActionSource.BEHAVIOR`), so an animation step is
        filtered by the safety policy exactly like a behaviour's own command. The energy
        gate reads the robot's personality, which is why this is built after it.
        """
        from robot.animation.engine import AnimationEngine, HandlePlayer
        from robot.animation.library import load_library
        from robot.state.actions import ActionSource

        engine = self._animations.get(robot_id)
        if engine is None:
            personality = self.personality(robot_id)
            engine = AnimationEngine(
                robot_id,
                HandlePlayer(self.actions.robot(robot_id).as_source(ActionSource.BEHAVIOR)),
                load_library(),
                events=self._events,
                energy=lambda: personality.energy,
                low_energy_threshold=personality.tuning.low_energy_threshold,
            )
            self._animations[robot_id] = engine
        return engine

    def vision(self, robot_id: str, **options: Any) -> VisionPipeline:
        """The vision pipeline for one robot, created on first use.

        Frames come from the device's own camera tool through
        :class:`~robot.vision.pipeline.McpFrameSource`, so vision never holds a reference
        to anything that can command a robot — it is handed one function that captures.
        The detector defaults to the null one: a deployment with no model gets a pipeline
        that runs, finds nothing, and leaves every behaviour that needs a person quiet.
        """
        from robot.vision.pipeline import McpFrameSource, VisionPipeline

        pipeline = self._vision.get(robot_id)
        if pipeline is None:
            source = McpFrameSource(
                lambda name, arguments, timeout=None: self.call_tool(
                    robot_id, name, arguments, timeout=timeout
                )
            )
            pipeline = VisionPipeline(
                robot_id, source, world=self._world, events=self._events, **options
            )
            self._vision[robot_id] = pipeline
        return pipeline

    def behavior(self, robot_id: str, *, seed: int = 0, autostart: bool = False) -> BehaviorEngine:
        """The behaviour engine for one robot, created on first use.

        Imported inside the method for the same reason as :attr:`actions`: the behaviour
        package imports the action layer, which imports this module's protocol, so a
        module-scope import here would be a cycle. Actions are attributed to
        :attr:`~robot.state.actions.ActionSource.BEHAVIOR`, which is what makes an incident
        log say the robot decided this by itself rather than that somebody asked for it.
        """
        from robot.behavior.engine import BehaviorEngine
        from robot.state.actions import ActionSource

        engine = self._behaviors.get(robot_id)
        if engine is None:
            personality = self.personality(robot_id)
            engine = BehaviorEngine(
                robot_id,
                self.actions.robot(robot_id).as_source(ActionSource.BEHAVIOR),
                self._world,
                events=self._events,
                mode=self.autonomy,
                seed=seed,
                drives=personality,
                animations=self.animations(robot_id),
            )
            self._behaviors[robot_id] = engine
            if autostart:
                engine.start()
        return engine

    async def get_state(self, robot_id: str) -> RobotState | None:
        """The world-model entry for one robot. What the safety policy is evaluated against."""
        return await self._store.get(robot_id)

    def channel(self, robot_id: str) -> ToolChannel | None:
        return self._channels.get(robot_id)

    async def attach(
        self,
        device: DeviceInfo,
        channel: ToolChannel | None = None,
        *,
        discover: bool = True,
    ) -> RobotState:
        """Register a connected device and kick off capability discovery.

        Returns as soon as the robot is registered — discovery runs in the background.
        A reconnecting device rediscovers: firmware may have changed between sessions.
        """
        if self._closed:
            raise RuntimeError("robot runtime is closed")
        # Subscribing needs a running loop, which __init__ cannot assume it has.
        self._world.attach(self._events)
        robot_id = device.robot_id
        state = await self._registry.register(device.identity(), device.connection())
        previous_channel = self._channels.get(robot_id)
        if channel is not None:
            self._channels[robot_id] = channel
            if previous_channel is not None and previous_channel is not channel:
                await _close_quietly(previous_channel, robot_id)
        if channel is not None and discover:
            # The channel confirms MCP support during discovery: at connection time the
            # device has not sent `hello` yet, so its feature block is not known here.
            self._start_discovery(robot_id)
        else:
            logger.info("robot %s: no tool channel, capabilities are feature flags only", robot_id)
            await self._registry.set_capabilities(robot_id, self._feature_capabilities(device))
        return state

    async def detach(
        self,
        robot_id: str,
        *,
        session_id: str | None = None,
        reason: DisconnectReason = DisconnectReason.CLIENT_CLOSED,
    ) -> bool:
        """Unregister a robot, cancel its discovery and close its tool channel.

        Idempotent, and a no-op when ``session_id`` belongs to a session that has already
        been replaced — double teardown is the normal flow in the inherited handler.
        """
        state = await self._registry.get(robot_id)
        if state is not None and session_id is not None and state.connection.session_id != session_id:
            return False
        removed = await self._registry.unregister(robot_id, session_id=session_id, reason=reason)
        if not removed:
            return False
        task = self._discovery.pop(robot_id, None)
        if task is not None and not task.done():
            task.cancel()
        channel = self._channels.pop(robot_id, None)
        if channel is not None:
            await _close_quietly(channel, robot_id)
        engine = self._behaviors.pop(robot_id, None)
        if engine is not None:
            await engine.aclose()
        pipeline = self._vision.pop(robot_id, None)
        if pipeline is not None:
            await pipeline.aclose()
        animation = self._animations.pop(robot_id, None)
        if animation is not None:
            await animation.aclose()
        personality = self._personalities.pop(robot_id, None)
        if personality is not None:
            # Writes the final snapshot: a robot that reconnects should not have lost its
            # personality because the session dropped.
            await personality.aclose()
        return True

    async def refresh_capabilities(self, robot_id: str) -> RobotCapabilities | None:
        """Run discovery now and wait for it. Returns ``None`` if there is no channel."""
        channel = self._channels.get(robot_id)
        if channel is None:
            return None
        capabilities = await asyncio.wait_for(channel.discover(), timeout=self._discovery_timeout)
        await self._registry.set_capabilities(robot_id, capabilities)
        return capabilities

    async def wait_for_discovery(self, robot_id: str) -> None:
        """Await the background discovery task, if one is running. Never raises."""
        task = self._discovery.get(robot_id)
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)

    async def update_telemetry(self, robot_id: str, telemetry: RobotTelemetry) -> RobotState | None:
        return await self._registry.update_state(robot_id, telemetry)

    async def call_tool(
        self,
        robot_id: str,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        """Call a device tool, announcing start, completion and failure on the bus.

        ``timeout`` is passed through to the channel; the channel's own default is short
        on purpose, because an unacknowledged device command is a failure, not a wait.
        """
        channel = self._channels.get(robot_id)
        if channel is None:
            raise UnknownRobotError(f"robot {robot_id} has no tool channel")
        if not await self._capabilities.has_tool(robot_id, name):
            raise McpError(f"robot {robot_id} does not publish a tool named {name!r}")
        call_id = uuid4().hex
        payload = dict(arguments or {})
        started = time.monotonic()
        await self._events.publish(
            ToolCallStarted(robot_id=robot_id, call_id=call_id, tool_name=name, arguments=payload)
        )
        try:
            result = await channel.call_tool(name, payload, timeout=timeout)
        except Exception as exc:
            await self._events.publish(
                ToolCallFailed(
                    robot_id=robot_id,
                    call_id=call_id,
                    tool_name=name,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )
            raise
        await self._events.publish(
            ToolCallCompleted(
                robot_id=robot_id,
                call_id=call_id,
                tool_name=name,
                result=result,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        )
        return result

    async def aclose(self) -> None:
        """Stop the executor, cancel discovery, close every channel, shut the bus down.

        The executor goes first: it unsubscribes from the bus and cancels its in-flight
        dispatches, so nothing is still trying to call a channel that is about to close.
        """
        self._closed = True
        engines, self._behaviors = list(self._behaviors.values()), {}
        for engine in engines:
            await engine.aclose()
        pipelines, self._vision = list(self._vision.values()), {}
        for pipeline in pipelines:
            await pipeline.aclose()
        animations, self._animations = list(self._animations.values()), {}
        for animation in animations:
            await animation.aclose()
        personalities, self._personalities = list(self._personalities.values()), {}
        for personality in personalities:
            await personality.aclose()
        await self._world.aclose()
        actions, self._actions = self._actions, None
        if actions is not None:
            await actions.aclose()
        tasks = list(self._discovery.values())
        self._discovery.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for robot_id, channel in list(self._channels.items()):
            await _close_quietly(channel, robot_id)
        self._channels.clear()
        await self._events.drain()
        await self._events.aclose()

    def _start_discovery(self, robot_id: str) -> None:
        previous = self._discovery.pop(robot_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        self._discovery[robot_id] = asyncio.create_task(
            self._discover(robot_id), name=f"robot-discovery-{robot_id}"
        )

    @staticmethod
    def _feature_capabilities(device: DeviceInfo) -> RobotCapabilities:
        return RobotCapabilities(
            mcp=device.supports_mcp,
            features=frozenset(str(key) for key, value in device.features.items() if value),
        )

    async def _discover(self, robot_id: str) -> None:
        try:
            await self.refresh_capabilities(robot_id)
        except asyncio.CancelledError:
            raise
        except McpUnsupportedError:
            logger.info("robot %s does not speak MCP; no tool capabilities", robot_id)
            state = await self._registry.get(robot_id)
            if state is not None:
                await self._registry.set_capabilities(robot_id, RobotCapabilities(mcp=False))
        except TimeoutError:
            logger.warning("robot %s: capability discovery timed out", robot_id)
        except Exception as exc:
            logger.warning("robot %s: capability discovery failed: %s", robot_id, exc)


async def _close_quietly(channel: ToolChannel, robot_id: str) -> None:
    try:
        await channel.aclose()
    except Exception as exc:  # a channel that cannot be closed must not break teardown
        logger.warning("robot %s: closing the tool channel failed: %s", robot_id, exc)


_runtime: RobotRuntime | None = None


def get_runtime() -> RobotRuntime:
    """The process-wide runtime, created on first use.

    One holder, because the inherited server has no place to hang a control plane. The
    class itself has no module state, so tests build their own instead of using this.
    """
    global _runtime
    if _runtime is None or _runtime.closed:
        _runtime = RobotRuntime()
    return _runtime


def set_runtime(runtime: RobotRuntime | None) -> None:
    """Install (or clear, with ``None``) the process-wide runtime. For tests and app startup."""
    global _runtime
    _runtime = runtime
