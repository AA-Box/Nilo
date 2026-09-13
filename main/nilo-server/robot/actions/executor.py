"""The action executor: admission, arbitration, dispatch and the lifecycle of every action.

Everything that reaches a motor goes through here, and it is the only thing that does.
The shape of one action's life:

    submit ─▶ safety.evaluate ─▶ queue ─▶ safety.evaluate again ─▶ claim ─▶ tools/call
                   │                             │                            │
               REJECTED                      REJECTED                    STARTING
                                                                              │
                                          device ack, action_id ──────▶  RUNNING
                                                                              │
                          notifications/motion_completed ──────────────▶ SUCCEEDED
                          notifications/motion_failed ─────────────────▶ FAILED
                          watchdog deadline ──────────────────────────▶ TIMED_OUT + stop
                          cancel / preempt / e-stop / disconnect ─────▶ CANCELLED

Five properties are load-bearing, and each is there because of something in the inherited
server rather than as a preference:

* **Submit never blocks on hardware.** It validates, admits, enqueues and returns. The
  chat loop awaits tool futures sequentially on a five-worker pool with no cancellation
  (``core/connection.py``), so a tool handler that waited for motion would pin a worker
  for the whole ``tool_call_timeout``.
* **Safety is evaluated twice** — once at submission and once immediately before the
  device call — because the world changes while an action sits in a queue. A cliff that
  appears after admission must still stop the move.
* **Every dispatch carries an explicit short timeout.** The inherited device-MCP default
  is 30 s and nothing overrides it (``core/providers/tools/device_mcp/mcp_executor.py``),
  which is two orders of magnitude too long for an acknowledgement.
* **Completion is correlated by the device's own action id.** A duplicate completion
  resolves to an action that was already retired, and is dropped rather than settling
  something twice.
* **The watchdog runs off this event loop.** See :mod:`robot.safety.watchdog`.

What it still is not: a guarantee. A stop this executor sends is a request that may never
arrive. The firmware watchdog is what actually stops the robot (docs/safety.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol

from robot.actions.model import (
    DISPATCH_TIMEOUT_S,
    ActionSpec,
    RobotAction,
    StopAction,
)
from robot.actions.queue import ResourceConflict, RobotActionQueue
from robot.actions.registry import RobotActionRegistry
from robot.events.bus import EventBus
from robot.events.types import (
    ActionFinished,
    ActionStarted,
    ActionSubmitted,
    EmergencyStopChanged,
    MotionCompleted,
    MotionFailed,
    RobotDisconnected,
    RobotEvent,
)
from robot.safety.estop import EmergencyStop
from robot.safety.limits import SafetyLimits
from robot.safety.policy import RobotSafetyPolicy, SafetyContext, SafetyDecision
from robot.state.actions import (
    MOTION_ACTIONS,
    ActionError,
    ActionPriority,
    ActionRecord,
    ActionSource,
    ActionStatus,
    ActionType,
    RejectionReason,
)
from robot.state.models import RobotState, utcnow

logger = logging.getLogger(__name__)

#: How often the supervisory sweep looks for a running motion whose link has gone quiet.
SUPERVISE_INTERVAL_S = 0.5


class ActionRuntime(Protocol):
    """The slice of :class:`robot.runtime.RobotRuntime` the executor uses.

    A structural type, not an import: ``robot/runtime.py`` creates the executor, so an
    import in the other direction would be a cycle. It is also what lets a unit test hand
    in a five-line fake with no registry, no bus and no device.
    """

    @property
    def events(self) -> EventBus: ...

    async def call_tool(
        self,
        robot_id: str,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str: ...

    async def get_state(self, robot_id: str) -> RobotState | None: ...


class RobotActionExecutor:
    """Owns every action for every robot in one process.

    Constructed per runtime. There is no module-level executor, because a shared queue is
    exactly the state that would make two robots in one process interfere.
    """

    def __init__(
        self,
        runtime: ActionRuntime,
        *,
        policy: RobotSafetyPolicy | None = None,
        limits: SafetyLimits | None = None,
        queue: RobotActionQueue | None = None,
        registry: RobotActionRegistry | None = None,
        watchdog: Any | None = None,
        start_watchdog: bool = True,
    ) -> None:
        from robot.safety.watchdog import Watchdog

        self._runtime = runtime
        self._policy = policy or RobotSafetyPolicy(limits or SafetyLimits())
        self._queue = queue or RobotActionQueue()
        self._registry = registry or RobotActionRegistry()
        self._watchdog: Watchdog = watchdog if watchdog is not None else Watchdog()
        self._start_watchdog = start_watchdog
        self._wake = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._subscriptions: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._motion_window: dict[str, deque[float]] = {}
        self._started = False
        self._closed = False

    # -- properties --------------------------------------------------------------------------

    @property
    def policy(self) -> RobotSafetyPolicy:
        return self._policy

    @property
    def limits(self) -> SafetyLimits:
        return self._policy.limits

    @property
    def estop(self) -> EmergencyStop:
        return self._policy.estop

    @property
    def queue(self) -> RobotActionQueue:
        return self._queue

    @property
    def registry(self) -> RobotActionRegistry:
        return self._registry

    @property
    def watchdog(self) -> Any:
        return self._watchdog

    @property
    def closed(self) -> bool:
        return self._closed

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to device events, start the pump and the watchdog. Idempotent."""
        if self._started or self._closed:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        events = self._runtime.events
        self._subscriptions.append(events.subscribe((MotionCompleted, MotionFailed), self._on_motion_event))
        self._subscriptions.append(events.subscribe(RobotDisconnected, self._on_disconnected))
        self._pump = asyncio.create_task(self._pump_loop(), name="robot-action-pump")
        self._watchdog.add_sweep(self._sweep_from_watchdog_thread, SUPERVISE_INTERVAL_S)
        if self._start_watchdog:
            self._watchdog.start()

    async def aclose(self) -> None:
        """Cancel everything in flight, stop the watchdog, unsubscribe. Idempotent.

        Deliberately does **not** try to stop the robots: teardown runs when the process
        is going away, and a stop issued from a dying process is the hope this design
        refuses to rely on. The firmware watchdog is what stops them.
        """
        if self._closed:
            return
        self._closed = True
        for subscription in self._subscriptions:
            with contextlib.suppress(Exception):
                self._runtime.events.unsubscribe(subscription)
        self._subscriptions.clear()
        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._watchdog.stop()

    # -- submission ---------------------------------------------------------------------------

    async def submit(
        self,
        spec: ActionSpec,
        robot_id: str,
        *,
        source: ActionSource = ActionSource.SYSTEM,
        priority: ActionPriority = ActionPriority.NORMAL,
        timeout_s: float | None = None,
        ttl_s: float | None = None,
    ) -> RobotAction:
        """Admit an action, or reject it. Returns immediately, in either case.

        The returned :class:`~robot.actions.model.RobotAction` is already terminal when
        safety refused it — ``action.status is ActionStatus.REJECTED`` and
        ``action.error.reason`` is the typed reason. Nothing here waits for hardware; a
        caller that wants the outcome awaits :meth:`~robot.actions.model.RobotAction.wait`.
        """
        if self._closed:
            raise RuntimeError("the action executor is closed")
        await self._ensure_started()
        action = RobotAction(
            spec,
            robot_id,
            source=source,
            priority=priority,
            timeout_s=timeout_s,
            ttl_s=ttl_s if ttl_s is not None else self.limits.action_ttl_s,
        )
        action.timeout_s = self._policy.clamp_timeout(action.timeout_s)
        self._registry.add(action)
        await self._publish(ActionSubmitted(robot_id=robot_id, action=action.record()))

        decision = await self._evaluate(action, at_dispatch=False)
        if not decision.allowed:
            await self._settle_rejected(action, decision)
            return action

        if spec.action_type is ActionType.STOP:
            # Rule 2 of the emergency stop: stop is never queued, so its latency is not a
            # function of queue depth. It preempts whatever holds the drive and goes out.
            await self._dispatch_stop(action)
            return action

        self._queue.enqueue(action)
        self._wake.set()
        return action

    async def cancel(self, action_id: str, *, reason: str = "cancelled by request") -> ActionRecord | None:
        """Cancel one action, pending or running. ``None`` if it is unknown or finished.

        Cancelling a *running* action is best effort on the hardware side: the action is
        marked ``CANCELLED`` here and a stop is attempted, but a command the device has
        already accepted cannot be recalled from the server.
        """
        action = self._registry.get(action_id)
        if action is None or action.is_terminal:
            return None
        was_running = action.status is not ActionStatus.PENDING
        record = await self._settle(
            action, ActionStatus.CANCELLED, error=ActionError(code="cancelled", message=reason)
        )
        if was_running and action.action_type in MOTION_ACTIONS:
            await self._attempt_stop(action.robot_id, reason)
        return record

    async def cancel_all(
        self, robot_id: str | None = None, *, reason: str = "cancelled by request", stop: bool = True
    ) -> tuple[ActionRecord, ...]:
        """Cancel everything pending and running, for one robot or for every robot."""
        targets = [
            action
            for action in self._registry.live(robot_id)
            if not action.is_terminal
        ]
        records: list[ActionRecord] = []
        motion_robots: set[str] = set()
        for action in targets:
            if action.status is not ActionStatus.PENDING and action.action_type in MOTION_ACTIONS:
                motion_robots.add(action.robot_id)
            record = await self._settle(
                action, ActionStatus.CANCELLED, error=ActionError(code="cancelled", message=reason)
            )
            if record is not None:
                records.append(record)
        if stop:
            for target in sorted(motion_robots):
                await self._attempt_stop(target, reason)
        return tuple(records)

    # -- query --------------------------------------------------------------------------------

    def query(self, action_id: str) -> ActionRecord | None:
        """The state of one action, live or retired."""
        return self._registry.record(action_id)

    def list_actions(
        self,
        robot_id: str | None = None,
        *,
        status: ActionStatus | None = None,
        include_history: bool = True,
        limit: int | None = None,
    ) -> tuple[ActionRecord, ...]:
        return self._registry.records(
            robot_id, status=status, include_history=include_history, limit=limit
        )

    def pending(self, robot_id: str | None = None) -> tuple[ActionRecord, ...]:
        return tuple(action.record() for action in self._queue.pending(robot_id))

    def running(self, robot_id: str | None = None) -> tuple[ActionRecord, ...]:
        return tuple(action.record() for action in self._queue.running(robot_id))

    def conflicts(self, spec: ActionSpec, robot_id: str) -> tuple[ActionRecord, ...]:
        """Which running actions would block this spec right now. Conflict detection, exposed."""
        probe = RobotAction(spec, robot_id)
        return tuple(action.record() for action in self._queue.conflicts(probe))

    def robot(self, robot_id: str) -> Any:
        """A semantic handle: ``await executor.robot(id).move(distance_mm=300)``."""
        from robot.actions.semantic import RobotHandle

        return RobotHandle(self, robot_id)

    # -- emergency stop -------------------------------------------------------------------------

    async def emergency_stop(
        self, robot_id: str, reason: str = "emergency stop", *, engaged_by: str = "system"
    ) -> ActionRecord | None:
        """Latch the robot, cancel everything, and try to stop it. Returns the stop's record.

        After this, nothing is admitted for that robot until
        :meth:`clear_emergency_stop` — including from the LLM, which has no path to
        override a safety rejection.
        """
        await self._ensure_started()
        state = self.estop.engage(robot_id, reason, engaged_by=engaged_by)
        await self._publish(
            EmergencyStopChanged(robot_id=robot_id, engaged=True, reason=reason, engaged_by=engaged_by)
        )
        await self.cancel_all(robot_id, reason=f"emergency stop: {reason}", stop=False)
        stop = RobotAction(
            StopAction(reason=state.reason),
            robot_id,
            source=ActionSource.SAFETY,
            priority=ActionPriority.EMERGENCY,
        )
        self._registry.add(stop)
        await self._publish(ActionSubmitted(robot_id=robot_id, action=stop.record()))
        await self._dispatch_stop(stop)
        return stop.record()

    async def clear_emergency_stop(self, robot_id: str) -> bool:
        """Release the latch. Returns whether it was engaged."""
        if not self.estop.clear(robot_id):
            return False
        await self._publish(EmergencyStopChanged(robot_id=robot_id, engaged=False, reason="cleared"))
        self._wake.set()
        return True

    def emergency_stopped(self, robot_id: str) -> bool:
        return self.estop.engaged(robot_id)

    # -- the dispatch pump -----------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        if not self._started and not self._closed:
            await self.start()

    async def _pump_loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            try:
                for robot_id in self._queue.robot_ids():
                    await self._dispatch_robot(robot_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("robot action pump failed; the queue is still live")

    async def _dispatch_robot(self, robot_id: str) -> None:
        """Dispatch everything that can run for one robot, preempting where allowed."""
        for _ in range(_MAX_PUMP_ROUNDS):
            action = self._queue.next_ready(robot_id)
            if action is None:
                if not await self._preempt_for_head(robot_id):
                    return
                continue
            self._queue.remove(action.action_id)
            decision = await self._evaluate(action, at_dispatch=True)
            if not decision.allowed:
                await self._settle_rejected(action, decision)
                continue
            try:
                self._queue.claim(action)
            except ResourceConflict:
                # Something claimed the resource while safety was being evaluated. Put the
                # action back at its place in the order and let the next round retry it.
                self._queue.enqueue(action)
                return
            action.transition(ActionStatus.STARTING)
            self._note_motion(action)
            self._spawn(self._dispatch(action), f"robot-action-{action.action_id[:8]}")

    async def _preempt_for_head(self, robot_id: str) -> bool:
        """Cancel the lower-priority holders blocking the head of the queue.

        Returns whether anything was cancelled, i.e. whether another dispatch round is
        worth running. Preemption is an observable transition on the victim, not a
        dropped request (docs/robot-architecture.md Sect. 2.8).
        """
        blocked = self._queue.next_blocked(robot_id)
        if blocked is None:
            return False
        victims = self._queue.preemptable(blocked)
        if not victims:
            return False
        for victim in victims:
            await self._settle(
                victim,
                ActionStatus.CANCELLED,
                error=ActionError(
                    code="preempted",
                    message=f"preempted by {blocked.action_type.value} at priority {int(blocked.priority)}",
                ),
            )
            if victim.action_type in MOTION_ACTIONS:
                await self._attempt_stop(victim.robot_id, "preempted by a higher-priority action")
        return True

    async def _dispatch(self, action: RobotAction) -> None:
        """One device call, with an explicit short timeout, off the pump."""
        await self._publish(ActionStarted(robot_id=action.robot_id, action=action.record()))
        try:
            raw = await self._runtime.call_tool(
                action.robot_id,
                action.spec.tool_name,
                action.spec.arguments(),
                timeout=DISPATCH_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await self._settle(
                action,
                ActionStatus.TIMED_OUT,
                error=ActionError(code="dispatch_timeout", message=f"the device did not acknowledge: {exc}"),
            )
            await self._attempt_stop(action.robot_id, "the device never acknowledged a command")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                await self._settle(
                    action,
                    ActionStatus.TIMED_OUT,
                    error=ActionError(code="dispatch_timeout", message=str(exc)),
                )
                await self._attempt_stop(action.robot_id, "the device never acknowledged a command")
                return
            await self._settle(
                action, ActionStatus.FAILED, error=ActionError(code=type(exc).__name__, message=str(exc))
            )
            return

        result = _as_result(raw)
        if not action.spec.awaits_completion:
            await self._settle(action, ActionStatus.SUCCEEDED, result=result)
            return

        device_action_id = result.get("action_id")
        if not isinstance(device_action_id, str) or not device_action_id:
            # Accepted, but with nothing to correlate a completion against. Supervising it
            # is impossible, so say so rather than leaving an action running forever.
            await self._settle(
                action,
                ActionStatus.FAILED,
                result=result,
                error=ActionError(
                    code="no_device_action_id",
                    message="the device accepted the command without returning an action_id",
                ),
            )
            await self._attempt_stop(action.robot_id, "an uncorrelatable command was accepted")
            return

        if action.is_terminal:
            # Cancelled while the device was being called. It has already been retired, so
            # binding now would leave a mapping nothing can ever resolve or clean up.
            logger.info(
                "robot %s: %s finished while the device was answering; not correlating %s",
                action.robot_id,
                action.spec.describe(),
                device_action_id,
            )
            return
        self._registry.bind_device_action(action, device_action_id)
        action.transition(ActionStatus.RUNNING, result=result)
        self._watchdog.arm(action.action_id, action.timeout_s, self._expire_from_watchdog_thread)

    async def _dispatch_stop(self, action: RobotAction) -> None:
        """Send a stop ahead of the queue, cancelling whatever holds the drive.

        Not routed through the pump on purpose: queueing a stop makes its latency depend
        on how much else is queued, which is the one thing a stop must not depend on.
        """
        reason = getattr(action.spec, "reason", "stop requested")
        for holder in self._queue.running(action.robot_id):
            await self._settle(
                holder, ActionStatus.CANCELLED, error=ActionError(code="stopped", message=reason)
            )
        with contextlib.suppress(ResourceConflict):
            self._queue.claim(action)
        action.transition(ActionStatus.STARTING)
        await self._dispatch(action)

    # -- settling ----------------------------------------------------------------------------------

    async def _settle(
        self,
        action: RobotAction,
        status: ActionStatus,
        *,
        result: dict[str, Any] | None = None,
        error: ActionError | None = None,
    ) -> ActionRecord | None:
        """Move an action to a terminal state exactly once, and release everything it held.

        Returns ``None`` for an action that was already terminal — which is how a
        duplicate completion, a cancel racing a timeout and a disconnect racing a cancel
        all resolve to "the first one won" instead of to an illegal transition.
        """
        if action.is_terminal:
            return None
        record = action.transition(status, result=result, error=error)
        self._watchdog.disarm(action.action_id)
        self._queue.release(action)
        self._queue.remove(action.action_id)
        self._registry.retire(action)
        await self._publish(ActionFinished(robot_id=action.robot_id, action=record))
        self._wake.set()
        return record

    async def _settle_rejected(self, action: RobotAction, decision: SafetyDecision) -> ActionRecord | None:
        assert decision.reason is not None
        if action.is_terminal:
            return None
        record = action.reject(decision.reason, decision.message)
        self._watchdog.disarm(action.action_id)
        self._queue.release(action)
        self._registry.retire(action)
        logger.info(
            "robot %s: %s rejected (%s): %s",
            action.robot_id,
            action.spec.describe(),
            decision.reason.value,
            decision.message,
        )
        await self._publish(ActionFinished(robot_id=action.robot_id, action=record))
        return record

    # -- device feedback ------------------------------------------------------------------------------

    async def _on_motion_event(self, event: MotionCompleted | MotionFailed) -> None:
        """Settle the action a device completion belongs to, or drop it.

        A completion for an unknown or already-retired action id is dropped with a log
        line and nothing else. That is the duplicate-completion path, and it is deliberate
        rather than incidental: firmware that retries a notification, or a reconnect that
        replays one, must not settle a second action.
        """
        action = self._registry.by_device_action(event.robot_id, event.action_id)
        if action is None:
            logger.debug(
                "robot %s: %s for device action %s matches no live action; dropping it",
                event.robot_id,
                type(event).__name__,
                event.action_id,
            )
            return
        if isinstance(event, MotionCompleted):
            await self._settle(action, ActionStatus.SUCCEEDED, result={"completed": True, "kind": event.kind})
            return
        status = ActionStatus.CANCELLED if event.reason == "cancelled" else ActionStatus.FAILED
        await self._settle(
            action, status, error=ActionError(code=event.reason, message=event.detail or event.reason)
        )

    async def _on_disconnected(self, event: RobotDisconnected) -> None:
        """A session dropped: cancel everything for that robot and try, once, to stop it.

        The stop attempt is expected to fail — the socket is gone, which is why the
        cancellation happens regardless of whether it succeeds. What stops the robot is
        the firmware watchdog noticing the same silence (docs/safety.md).
        """
        cancelled = await self.cancel_all(
            event.robot_id, reason=f"the robot disconnected ({event.reason.value})", stop=False
        )
        if cancelled:
            logger.warning(
                "robot %s disconnected: %d action(s) cancelled; a stop cannot be delivered over a "
                "closed session, so the firmware watchdog is what stops the robot",
                event.robot_id,
                len(cancelled),
            )

    async def _attempt_stop(self, robot_id: str, reason: str) -> bool:
        """Ask the device to stop. Best effort, and honest about it in the log."""
        try:
            await self._runtime.call_tool(
                robot_id, StopAction.tool_name, {}, timeout=DISPATCH_TIMEOUT_S
            )
        except Exception as exc:
            logger.warning(
                "robot %s: the stop after %r did not reach the device (%s). The backend cannot "
                "guarantee a stop; the firmware watchdog can",
                robot_id,
                reason,
                exc,
            )
            return False
        logger.info("robot %s: stop dispatched after %s", robot_id, reason)
        return True

    # -- supervision ----------------------------------------------------------------------------------

    def _expire_from_watchdog_thread(self, action_id: str) -> None:
        """Watchdog-thread entry point. Hops to the event loop and returns immediately."""
        self._from_watchdog_thread(self._time_out(action_id))

    def _sweep_from_watchdog_thread(self) -> None:
        self._from_watchdog_thread(self.supervise())

    def _from_watchdog_thread(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or self._closed:
            coroutine.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:  # pragma: no cover - the loop shut down between the checks
            coroutine.close()

    async def _time_out(self, action_id: str) -> None:
        """A deadline passed with no completion from the device."""
        action = self._registry.get(action_id)
        if action is None or action.is_terminal:
            return
        logger.warning(
            "robot %s: action %s (%s) did not report completion within %.1fs; marking it TIMED_OUT "
            "and attempting a stop",
            action.robot_id,
            action_id,
            action.spec.describe(),
            action.timeout_s,
        )
        await self._settle(
            action,
            ActionStatus.TIMED_OUT,
            error=ActionError(
                code="timeout",
                message=f"no completion within {action.timeout_s:.1f}s",
            ),
        )
        await self._attempt_stop(action.robot_id, "an action timed out")

    async def supervise(self) -> None:
        """Stop motion that is already running when the world says it must not continue.

        Admission checks a request; this checks a robot. It is how "stop motion when the
        device disconnects" and "stop motion when the heartbeat expires" are enforced
        against an action that was perfectly legal when it was admitted.

        Public because it is the whole supervisory pass minus the thread: the watchdog
        calls it on a timer, a management API can call it on demand, and a test can call
        it directly instead of waiting out an interval.
        """
        if self._closed:
            return
        for action in self._registry.live(status=ActionStatus.RUNNING):
            if action.action_type not in MOTION_ACTIONS:
                continue
            state = await self._runtime.get_state(action.robot_id)
            hazard = self._policy.hazard_for(state)
            if hazard is None:
                continue
            logger.warning(
                "robot %s: %s while %s was running; cancelling it",
                action.robot_id,
                hazard.value,
                action.spec.describe(),
            )
            await self._settle(
                action,
                ActionStatus.CANCELLED,
                error=ActionError.rejected(hazard, f"supervisor stopped a running motion: {hazard.value}"),
            )
            if hazard is not RejectionReason.DEVICE_DISCONNECTED:
                await self._attempt_stop(action.robot_id, hazard.value)

    # -- helpers ----------------------------------------------------------------------------------------

    async def _evaluate(self, action: RobotAction, *, at_dispatch: bool) -> SafetyDecision:
        state = await self._runtime.get_state(action.robot_id)
        now = utcnow()
        return self._policy.evaluate(
            action.spec,
            action.source,
            SafetyContext(
                robot_id=action.robot_id,
                state=state,
                now=now,
                age_s=action.record().age_s(now),
                recent_motions=self._recent_motions(action.robot_id),
                at_dispatch=at_dispatch,
            ),
        )

    def _recent_motions(self, robot_id: str) -> int:
        """How many motion commands were admitted inside the rate window."""
        window = self._motion_window.get(robot_id)
        if not window:
            return 0
        cutoff = time.monotonic() - self.limits.rate_window_s
        while window and window[0] < cutoff:
            window.popleft()
        return len(window)

    def _note_motion(self, action: RobotAction) -> None:
        if action.action_type not in MOTION_ACTIONS:
            return
        self._motion_window.setdefault(action.robot_id, deque()).append(time.monotonic())

    async def _publish(self, event: RobotEvent) -> None:
        try:
            await self._runtime.events.publish(event)
        except Exception as exc:  # a closed bus must not break an action's lifecycle
            logger.debug("robot %s: could not publish %s: %s", event.robot_id, event.name, exc)

    def _spawn(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def __repr__(self) -> str:
        return (
            f"<RobotActionExecutor live={len(self._registry)} queued={len(self._queue)} "
            f"estop={list(self.estop.engaged_ids())}>"
        )


#: Upper bound on dispatch rounds per robot per pump wake-up. A guard against a
#: pathological preempt/enqueue cycle, not a throughput limit: a round dispatches an
#: action, and nothing realistic queues a hundred at once for one robot.
_MAX_PUMP_ROUNDS = 100


def _as_result(raw: str) -> dict[str, Any]:
    """A device tool reply as a mapping. Non-JSON replies become ``{"text": ...}``.

    Firmware is allowed to answer with plain text; only the fields the executor needs
    (``action_id``) have to be structured, and their absence is handled explicitly.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"text": raw}
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return {"text": raw}


def _is_timeout(exc: BaseException) -> bool:
    """Whether an exception from the tool channel means "no answer", not "bad answer"."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ in {"McpTimeoutError", "TimeoutError"}


__all__ = ["SUPERVISE_INTERVAL_S", "ActionRuntime", "RobotActionExecutor"]
