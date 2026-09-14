"""The robot agent: the model, wrapped in everything that keeps it out of trouble.

    agent = RobotAgent("nilo-sim-01", runtime, llm=provider)
    turn = await agent.respond("come closer", person_id="ahmad")
    print(turn.text)                   # "Okay."
    print(turn.tool_calls)             # ("robot_move",)

The agent owns **conversation, interpretation, planning, tool selection and wording**.
It owns none of the things that have to be right every time:

    PID loops            firmware
    raw motors           firmware
    safety               robot/safety, below the action layer
    behaviour scheduling robot/behavior, which never consults a model
    vision tracking      robot/vision, a loop with no model in it
    timing               the action watchdog, on its own thread

That split is not a preference. A language model is a component that is occasionally
wrong, occasionally slow, and occasionally unavailable, and every one of those is
survivable for a conversation and fatal for a control loop. When the model is gone the
robot still senses, still behaves, still stops — it just has less to say, and
:meth:`RobotAgent.respond` says so instead of raising (:data:`FALLBACK_REPLIES`).

**Reuse, not a second stack.** ``llm`` is any object with the inherited provider's
``response_with_functions`` — every provider under ``core/providers/llm/`` already is one,
and so is a twenty-line fake in a test. There is no second client, no second retry policy
and no second place that knows an API key.

**The provider is synchronous.** The inherited providers are blocking generators called
from a worker thread. :func:`stream_sync` pumps one into an asyncio queue so the agent can
stay async and still stream tokens as they arrive — which is what lets Phase 9 start
speaking before the model has finished thinking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from robot.agent.context import RobotContext, build_context
from robot.agent.permissions import ToolPolicy, TurnOrigin
from robot.agent.speech import SpeakIntent
from robot.agent.tools import RobotToolkit, ToolOutcome
from robot.events.types import (
    AgentTurnCompleted,
    AgentTurnFailed,
    AgentTurnStarted,
    ToolCallRefused,
)
from robot.state.models import utcnow

logger = logging.getLogger(__name__)

#: How many model round trips one turn may take. Each one is a tool result fed back in.
#: Three is enough for "look, then move, then answer" and short enough that a model stuck
#: in a loop stops costing money within a few seconds.
MAX_TOOL_ROUNDS = 3

#: How many messages of conversation the agent carries. Long enough to hold a real
#: exchange, short enough that the context does not grow without bound.
MAX_HISTORY = 20

#: What the robot says when the model cannot be reached. Keyed by what went wrong, so a
#: timeout and a missing provider do not produce the same sentence.
FALLBACK_REPLIES: dict[str, str] = {
    "unavailable": "I cannot think of anything to say right now.",
    "timeout": "Sorry, I took too long thinking about that.",
    "error": "Something went wrong while I was thinking.",
}

#: How long one model turn may take before it is abandoned and the fallback speaks.
DEFAULT_LLM_TIMEOUT_S = 30.0


class LLMProvider(Protocol):
    """The slice of the inherited LLM provider the agent uses.

    Structural, so any of ``core/providers/llm/*`` satisfies it without inheriting from
    anything here, and so a test satisfies it in twenty lines with no network.
    """

    def response_with_functions(
        self, session_id: str, dialogue: list[dict[str, Any]], functions: list[dict[str, Any]] | None = None
    ) -> Iterable[tuple[Any, Any]]: ...


class LLMUnavailable(RuntimeError):
    """The model could not be reached at all. Caught by the agent, never raised to a caller."""


@dataclass
class Conversation:
    """What has been said, and to whom. Survives an interruption on purpose."""

    robot_id: str
    person_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    #: Set while a turn is in flight and cleared when it settles. A barge-in reads this to
    #: know there was something to interrupt.
    active_turn: str | None = None

    def add(self, role: str, content: str, **extra: Any) -> None:
        if not content and not extra:
            return
        self.messages.append({"role": role, "content": content, **extra})
        del self.messages[:-MAX_HISTORY]

    def switch_person(self, person_id: str | None) -> None:
        """A different person is talking. Keep the transcript; note the change.

        The history is not cleared: two people in one room are one conversation, and a
        robot that forgets the first one the moment the second speaks is worse company.
        """
        if person_id == self.person_id:
            return
        self.person_id = person_id
        if person_id:
            self.add("system", f"[{person_id} is now the one speaking]")

    def dialogue(self, system_prompt: str) -> list[dict[str, Any]]:
        return [{"role": "system", "content": system_prompt}, *self.messages]


@dataclass(frozen=True)
class AgentTurn:
    """The result of one turn: what was said, what was done, and what went wrong."""

    turn_id: str
    text: str = ""
    tool_calls: tuple[str, ...] = ()
    outcomes: tuple[ToolOutcome, ...] = ()
    interrupted: bool = False
    fallback: str = ""
    context: RobotContext | None = None

    @property
    def used_fallback(self) -> bool:
        return bool(self.fallback)

    @property
    def refusals(self) -> tuple[ToolOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.refused)


class RobotAgent:
    """Conversation, interpretation, planning and tool selection for one robot."""

    def __init__(
        self,
        robot_id: str,
        runtime: Any,
        *,
        llm: LLMProvider | None = None,
        toolkit: RobotToolkit | None = None,
        policy: ToolPolicy | None = None,
        memory: Any = None,
        animations: Any = None,
        session_id: str = "",
        llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.robot_id = robot_id
        self.runtime = runtime
        self.llm = llm
        self.memory = memory
        self.session_id = session_id or uuid4().hex
        self.llm_timeout_s = llm_timeout_s
        self.max_tool_rounds = max_tool_rounds
        self.toolkit = toolkit or RobotToolkit(
            runtime, robot_id, policy=policy, memory=memory, animations=animations
        )
        self.conversation = Conversation(robot_id)
        self._cancel = asyncio.Event()

    # -- conversation -------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether there is a model behind this agent at all."""
        return self.llm is not None

    async def respond(
        self,
        utterance: str,
        *,
        person_id: str | None = None,
        origin: TurnOrigin = TurnOrigin.USER,
        on_text: Callable[[str], Any] | None = None,
    ) -> AgentTurn:
        """One full turn: context, model, tools, model again, answer.

        ``on_text`` is called with each chunk of the spoken reply as it arrives, so a
        caller can start speaking before the model has finished. It may be a coroutine
        function; exceptions from it are logged and do not fail the turn.
        """
        chunks: list[str] = []

        async def collect(chunk: str) -> None:
            chunks.append(chunk)
            if on_text is not None:
                await _maybe_await(on_text(chunk))

        turn = await self._run_turn(utterance, person_id=person_id, origin=origin, on_text=collect)
        return turn

    async def speak_intent(self, intent: SpeakIntent) -> AgentTurn:
        """Answer a structured request from the behaviour engine or a safety announcement.

        A verbatim intent never reaches the model: a safety line is said exactly as
        written, and a model in that path is a model that can paraphrase a warning.
        """
        if intent.verbatim:
            turn = AgentTurn(turn_id=intent.intent_id, text=intent.text)
            self.conversation.add("assistant", intent.text)
            return turn
        prompt = intent.prompt or _intent_prompt(intent)
        return await self._run_turn(
            prompt,
            person_id=intent.target_person_id,
            origin=intent.origin,
            on_text=None,
            # A system instruction, not a person speaking: nobody said this out loud, and
            # a transcript that claims somebody did will be repeated back later.
            utterance_role="system",
        )

    async def interrupt(self, reason: str = "the person started speaking") -> bool:
        """Cancel the turn in flight. Conversation state is kept, not thrown away.

        Returns whether there was anything to interrupt. The partial reply stays in the
        transcript, marked, because the robot did say those words out loud and a model
        that is told it said nothing will repeat itself.
        """
        if self.conversation.active_turn is None:
            return False
        self._cancel.set()
        logger.info("robot %s: conversation interrupted (%s)", self.robot_id, reason)
        return True

    def reset(self) -> None:
        """Forget the transcript. The person, the tools and the policy are unchanged."""
        self.conversation = Conversation(self.robot_id, person_id=self.conversation.person_id)

    # -- the turn ------------------------------------------------------------------------------

    async def _run_turn(
        self,
        utterance: str,
        *,
        person_id: str | None,
        origin: TurnOrigin,
        on_text: Callable[[str], Any] | None,
        utterance_role: str = "user",
    ) -> AgentTurn:
        turn_id = uuid4().hex
        self._cancel.clear()
        self.conversation.switch_person(person_id)
        self.conversation.active_turn = turn_id
        self.conversation.turns += 1
        self.conversation.add(utterance_role, utterance)
        await self._publish(
            AgentTurnStarted(
                robot_id=self.robot_id,
                turn_id=turn_id,
                origin=origin.value,
                person_id=person_id,
                utterance=utterance,
            )
        )
        try:
            return await self._think(turn_id, utterance, person_id, origin, on_text)
        finally:
            self.conversation.active_turn = None

    async def _think(
        self,
        turn_id: str,
        utterance: str,
        person_id: str | None,
        origin: TurnOrigin,
        on_text: Callable[[str], Any] | None,
    ) -> AgentTurn:
        tools = self.toolkit.available(origin)
        context = await build_context(
            self.runtime,
            self.robot_id,
            person_id=person_id,
            query=utterance,
            tools=tools,
            origin=origin,
            memory=self.memory,
        )
        if self.llm is None:
            return await self._fallback(turn_id, "unavailable", context)

        functions = [
            description
            for description in self.toolkit.function_descriptions()
            if description["function"]["name"] in tools
        ]
        spoken: list[str] = []
        outcomes: list[ToolOutcome] = []
        called: list[str] = []

        for round_index in range(self.max_tool_rounds):
            # Tools are offered on the first round always, and withheld on the last one so
            # a model that keeps calling tools is forced to produce an answer instead of
            # looping until the timeout.
            offer_tools = round_index == 0 or round_index < self.max_tool_rounds - 1
            try:
                text, tool_calls = await self._one_round(
                    context, functions if offer_tools else None, on_text
                )
            except asyncio.CancelledError:
                raise
            except LLMUnavailable as exc:
                logger.warning("robot %s: the model is unavailable: %s", self.robot_id, exc)
                return await self._fallback(turn_id, "unavailable", context, spoken)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("robot %s: the model did not answer in %.0fs", self.robot_id, self.llm_timeout_s)
                return await self._fallback(turn_id, "timeout", context, spoken)
            except Exception as exc:
                logger.exception("robot %s: the model failed", self.robot_id)
                await self._publish(
                    AgentTurnFailed(robot_id=self.robot_id, turn_id=turn_id, error=f"{type(exc).__name__}: {exc}")
                )
                return await self._fallback(turn_id, "error", context, spoken)

            if text:
                spoken.append(text)
            if self._cancel.is_set():
                return self._interrupted(turn_id, spoken, outcomes, called, context)
            if not tool_calls:
                break

            self.conversation.messages.append(
                {"role": "assistant", "content": text, "tool_calls": _as_tool_calls(tool_calls)}
            )
            for call in tool_calls:
                outcome = await self.toolkit.call(
                    call["name"], _arguments(call), origin=origin, person_id=person_id
                )
                called.append(call["name"])
                outcomes.append(outcome)
                if outcome.refused:
                    await self._publish(
                        ToolCallRefused(
                            robot_id=self.robot_id,
                            turn_id=turn_id,
                            tool_name=call["name"],
                            refused_by=outcome.refused_by,
                            reason=outcome.error,
                        )
                    )
                self.conversation.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or call["name"],
                        "name": call["name"],
                        "content": outcome.as_text(),
                    }
                )
            del self.conversation.messages[:-MAX_HISTORY]

        reply = "".join(spoken).strip()
        if reply:
            self.conversation.add("assistant", reply)
        turn = AgentTurn(
            turn_id=turn_id,
            text=reply,
            tool_calls=tuple(called),
            outcomes=tuple(outcomes),
            context=context,
        )
        await self._publish(
            AgentTurnCompleted(
                robot_id=self.robot_id,
                turn_id=turn_id,
                text=reply,
                tool_calls=tuple(called),
                refusals=tuple(outcome.tool for outcome in turn.refusals),
            )
        )
        return turn

    async def _one_round(
        self,
        context: RobotContext,
        functions: list[dict[str, Any]] | None,
        on_text: Callable[[str], Any] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """One model call. Returns the spoken text and any tool calls it asked for."""
        provider = self.llm
        if provider is None:
            raise LLMUnavailable("no LLM provider is configured")
        dialogue = self.conversation.dialogue(context.render())
        pieces: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        def start() -> Iterable[tuple[Any, Any]]:
            try:
                return provider.response_with_functions(self.session_id, dialogue, functions=functions)
            except Exception as exc:  # a provider that will not even start is unavailable
                raise LLMUnavailable(str(exc)) from exc

        async with asyncio.timeout(self.llm_timeout_s):
            async for content, delta in stream_sync(start, self._cancel):
                if delta:
                    merge_tool_calls(tool_calls, delta)
                if content and not tool_calls:
                    pieces.append(content)
                    if on_text is not None:
                        await _maybe_await(on_text(content))
                if self._cancel.is_set():
                    break
        return "".join(pieces), tool_calls

    def _interrupted(
        self,
        turn_id: str,
        spoken: list[str],
        outcomes: list[ToolOutcome],
        called: list[str],
        context: RobotContext,
    ) -> AgentTurn:
        partial = "".join(spoken).strip()
        if partial:
            # Recorded as said, because it was: the robot spoke those words out loud
            # before it was cut off, and a model told it said nothing repeats itself.
            self.conversation.add("assistant", partial + " [interrupted]")
        return AgentTurn(
            turn_id=turn_id,
            text=partial,
            tool_calls=tuple(called),
            outcomes=tuple(outcomes),
            interrupted=True,
            context=context,
        )

    async def _fallback(
        self, turn_id: str, kind: str, context: RobotContext, spoken: list[str] | None = None
    ) -> AgentTurn:
        """What the robot says when there is no model. The robot keeps working regardless.

        Nothing else is degraded by this: the behaviour engine, the safety policy, the
        world model and the action layer have no model in them and carry on unchanged.
        """
        text = "".join(spoken or []).strip() or FALLBACK_REPLIES.get(kind, FALLBACK_REPLIES["error"])
        self.conversation.add("assistant", text)
        await self._publish(
            AgentTurnCompleted(robot_id=self.robot_id, turn_id=turn_id, text=text, fallback=kind)
        )
        return AgentTurn(turn_id=turn_id, text=text, fallback=kind, context=context)

    async def _publish(self, event: Any) -> None:
        bus = getattr(self.runtime, "events", None)
        if bus is None:
            return
        try:
            await bus.publish(event)
        except Exception as exc:  # an event nobody can publish must not fail a turn
            logger.warning("robot %s: publishing %s failed: %s", self.robot_id, type(event).__name__, exc)

    def __repr__(self) -> str:
        model = "none" if self.llm is None else type(self.llm).__name__
        return f"<RobotAgent {self.robot_id} llm={model} turns={self.conversation.turns}>"


class AgentSpeaker:
    """The :class:`~robot.agent.speech.Speaker` the arbiter drives by default.

    Composes the words with the agent and hands them to ``say``. In Phase 8 ``say`` is
    whatever a caller passes — a log line, a test recorder; Phase 9 passes the
    text-to-speech pipeline. The arbiter never knows which, and the agent never knows
    there is an arbiter.
    """

    def __init__(
        self,
        agent: RobotAgent,
        say: Callable[[str, SpeakIntent], Any] | None = None,
    ) -> None:
        self.agent = agent
        self._say = say
        #: Everything actually said, newest last. Bounded, and useful to a dashboard.
        self.said: list[tuple[str, str]] = []

    async def speak(self, intent: SpeakIntent) -> None:
        turn = await self.agent.speak_intent(intent)
        if not turn.text:
            return
        self.said.append((intent.reason, turn.text))
        del self.said[:-MAX_HISTORY]
        if self._say is not None:
            await _maybe_await(self._say(turn.text, intent))

    async def interrupt(self, reason: str) -> None:
        await self.agent.interrupt(reason)


# -- helpers --------------------------------------------------------------------------------------


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value


async def stream_sync(
    factory: Callable[[], Iterable[tuple[Any, Any]]], cancel: asyncio.Event
) -> AsyncIterator[tuple[Any, Any]]:
    """Drive a blocking generator from async code, yielding as items arrive.

    The inherited LLM providers are synchronous generators that block on the network. A
    thread pumps one into a bounded queue; this coroutine yields from the queue. The
    thread is not cancellable — a blocking ``recv`` is not — so a cancel stops *consuming*
    and the thread drains into a queue nobody reads and exits on its own.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
    done = object()
    # A separate flag from ``cancel``: this one says "the consumer has gone away", which
    # is true at the end of every stream. Setting the caller's cancel event here would
    # make a normal completion look like an interruption.
    stopped = threading.Event()

    def pump() -> None:
        try:
            iterator: Iterator[tuple[Any, Any]] = iter(factory())
            for item in iterator:
                loop.call_soon_threadsafe(_offer, queue, item)
                if cancel.is_set() or stopped.is_set():
                    break
        except BaseException as exc:  # noqa: BLE001 - re-raised on the consumer side
            loop.call_soon_threadsafe(_offer, queue, exc)
        finally:
            loop.call_soon_threadsafe(_offer, queue, done)

    loop.run_in_executor(None, pump)
    try:
        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()


def _offer(queue: asyncio.Queue[Any], item: Any) -> None:
    """Put without blocking, dropping the oldest item when the consumer has stopped."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:  # pragma: no cover - only when the consumer has gone away
        with_drop = queue.get_nowait
        with_drop()
        queue.put_nowait(item)


def merge_tool_calls(collected: list[dict[str, Any]], delta: Any) -> list[dict[str, Any]]:
    """Fold a streaming tool-call delta into the list being assembled.

    Accepts both the OpenAI SDK's delta objects and plain dictionaries, because the
    inherited providers yield the former and a fake yields the latter.
    """
    for call in delta or ():
        index = _get(call, "index")
        function = _get(call, "function") or {}
        name = _get(function, "name") or ""
        arguments = _get(function, "arguments") or ""
        identifier = _get(call, "id") or ""
        if index is None:
            index = len(collected) if name else max(0, len(collected) - 1)
        while index >= len(collected):
            collected.append({"id": "", "name": "", "arguments": ""})
        entry = collected[index]
        if identifier:
            entry["id"] = identifier
        if name:
            entry["name"] = name
        if arguments:
            entry["arguments"] += arguments
    return collected


def _get(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    """A tool call's arguments as a dictionary. A malformed blob becomes an empty one.

    Returning ``{}`` rather than raising is deliberate: the toolkit validates, and a
    missing required argument produces an error the model can read and correct. A parse
    exception here would be an error nobody can see.
    """
    raw = call.get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("robot agent: could not parse tool arguments %r", raw)
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _as_tool_calls(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The assistant message's ``tool_calls`` block, in the shape providers expect back."""
    return [
        {
            "id": call.get("id") or call["name"],
            "type": "function",
            "function": {"name": call["name"], "arguments": call.get("arguments") or "{}"},
        }
        for call in calls
    ]


def _intent_prompt(intent: SpeakIntent) -> str:
    """Turn a structured speak intent into the one sentence the model is asked to answer."""
    who = f" to {intent.target_person_id}" if intent.target_person_id else ""
    when = utcnow().strftime("%H:%M")
    return (
        f"[{when}] Say something{who} because: {intent.reason}. "
        f"Style: {intent.style}. One short sentence, spoken out loud."
    )


__all__ = [
    "DEFAULT_LLM_TIMEOUT_S",
    "FALLBACK_REPLIES",
    "MAX_HISTORY",
    "MAX_TOOL_ROUNDS",
    "AgentSpeaker",
    "AgentTurn",
    "Conversation",
    "LLMProvider",
    "LLMUnavailable",
    "RobotAgent",
    "merge_tool_calls",
    "stream_sync",
]
