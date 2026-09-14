"""The wiring: traits plus control variables plus the bus, for one robot.

:class:`PersonalityModel` is what the rest of the system holds. It owns an
:class:`~robot.personality.emotion.EmotionEngine`, subscribes to the events that should
move it, persists the traits and an occasional coarse snapshot, and exposes the current
values as something the behaviour engine can read without importing this package.

    personality = PersonalityModel("nilo-sim-01", traits=load_traits())
    personality.attach(runtime.events)
    engine.set_drives(personality)        # the behaviour engine reads it every tick

What moves what (the mapping is data, in :attr:`PersonalityModel.stimulus_map`, so a
deployment can change it without touching this file):

    a motion fails                 -> confidence falls a little
    a motion is refused for a cliff-> arousal rises
    the touch sensor asserts       -> valence rises, social need falls, boredom resets
    the battery says charging      -> energy climbs on the fast half-life
    a greeting completes           -> a positive interaction
    an investigation completes     -> curiosity is satisfied

Nothing here decides anything. It produces numbers that make some behaviours more likely
than others, one layer up, and it cannot reach the safety policy at all.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from robot.events.bus import EventBus, Subscription
from robot.events.types import (
    ActionFinished,
    BatteryUpdated,
    BehaviorCompleted,
    RobotEvent,
    SensorUpdated,
)
from robot.personality.emotion import EmotionalState, EmotionEngine, EmotionTuning, Stimulus
from robot.personality.store import PersonalityStore
from robot.personality.traits import PersonalityTraits
from robot.state.actions import ActionStatus, RejectionReason

logger = logging.getLogger(__name__)

#: Behaviours whose completion is itself a stimulus. Keyed by behaviour name, so a
#: deployment with its own behaviours extends the map rather than editing the model.
DEFAULT_BEHAVIOR_STIMULI: dict[str, Stimulus] = {
    "greet_person": Stimulus.POSITIVE_INTERACTION,
    "react_to_touch": Stimulus.TOUCH,
    "react_to_sound": Stimulus.LOUD_SOUND,
    "investigate_object": Stimulus.CURIOSITY_SATISFIED,
    "explore": Stimulus.CURIOSITY_SATISFIED,
    "look_around": Stimulus.CURIOSITY_SATISFIED,
    "charging": Stimulus.CHARGING,
    "wake": Stimulus.RESTED,
}

#: Rejections that mean "the world got in the way" rather than "the request was silly".
HAZARD_REASONS: frozenset[RejectionReason] = frozenset(
    {
        RejectionReason.CLIFF_HAZARD,
        RejectionReason.BUMP_HAZARD,
        RejectionReason.OBSTACLE_TOO_CLOSE,
        RejectionReason.ROBOT_LIFTED,
    }
)


class PersonalityModel:
    """One robot's personality and internal state, kept current by the event bus.

    Reads as :class:`robot.behavior.base.Drives`: the behaviour engine holds one of these
    and asks for ``.curiosity``, ``.boredom`` and friends every tick, which is why the
    properties decay on read rather than on a timer. No background task, nothing to leak.
    """

    def __init__(
        self,
        robot_id: str,
        *,
        traits: PersonalityTraits | None = None,
        tuning: EmotionTuning | None = None,
        store: PersonalityStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        state: EmotionalState | None = None,
        restore: bool = True,
    ) -> None:
        self.robot_id = robot_id
        self._clock = clock
        self._store = store
        self.stimulus_map = dict(DEFAULT_BEHAVIOR_STIMULI)
        restored_traits, restored_state = self._restore() if (restore and store is not None) else (None, None)
        self.engine = EmotionEngine(
            traits or restored_traits or PersonalityTraits(),
            tuning=tuning,
            state=state or restored_state,
            now=clock(),
        )
        self._subscriptions: list[Subscription] = []
        self._bus: EventBus | None = None
        self._hazard_asserted = False

    # -- reading -------------------------------------------------------------------------------

    @property
    def traits(self) -> PersonalityTraits:
        return self.engine.traits

    @property
    def state(self) -> EmotionalState:
        """The control variables, decayed to now."""
        return self.engine.update(self._clock())

    @property
    def tuning(self) -> EmotionTuning:
        return self.engine.tuning

    # The Drives protocol. Each one decays on read, so a caller that reads three of them in
    # a scoring pass sees one consistent instant — ``update`` is idempotent within a tick.
    @property
    def curiosity(self) -> float:
        return self.state.curiosity

    @property
    def boredom(self) -> float:
        return self.state.boredom

    @property
    def social_need(self) -> float:
        return self.state.social_need

    @property
    def energy(self) -> float:
        return self.state.energy

    @property
    def valence(self) -> float:
        return self.state.valence

    @property
    def arousal(self) -> float:
        return self.state.arousal

    @property
    def confidence(self) -> float:
        return self.state.confidence

    @property
    def low_energy(self) -> bool:
        """Whether energetic animations should be suppressed right now."""
        return self.state.energy < self.tuning.low_energy_threshold

    # -- writing --------------------------------------------------------------------------------

    def record(self, stimulus: Stimulus | str, *, strength: float = 1.0) -> EmotionalState:
        """Apply a stimulus now. The manual path, for the session seam and for tests."""
        return self.engine.record(stimulus, strength=strength, now=self._clock())

    def set_traits(self, traits: PersonalityTraits, *, persist: bool = True) -> None:
        """Change the personality and (by default) write it down immediately."""
        self.engine.set_traits(traits)
        if persist and self._store is not None:
            self._store.save(self.robot_id, traits, self.state, now=self._clock(), force=True)

    def save(self, *, force: bool = False) -> bool:
        """Persist traits and a coarse snapshot. Throttled unless ``force``."""
        if self._store is None:
            return False
        return self._store.save(
            self.robot_id, self.traits, self.state, now=self._clock(), force=force
        )

    # -- the event seam ---------------------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        """Subscribe to the events that move the control variables. Idempotent per bus."""
        if self._bus is bus and self._subscriptions:
            return
        self.detach()
        self._bus = bus
        self._subscriptions.append(
            bus.subscribe((ActionFinished, SensorUpdated, BatteryUpdated, BehaviorCompleted), self._on_event)
        )

    def detach(self) -> None:
        bus, self._bus = self._bus, None
        for subscription in self._subscriptions:
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.unsubscribe(subscription)
        self._subscriptions.clear()

    async def aclose(self) -> None:
        """Unsubscribe and write the final snapshot."""
        self.detach()
        self.save(force=True)

    # -- internals -----------------------------------------------------------------------------------

    def _restore(self) -> tuple[PersonalityTraits | None, EmotionalState | None]:
        assert self._store is not None
        record = self._store.load(self.robot_id)
        if record is None:
            return None, None
        logger.info("robot %s: personality restored (%s)", self.robot_id, record.traits.describe())
        return record.traits, record.state

    async def _on_event(self, event: RobotEvent) -> None:
        try:
            self._fold(event)
        except Exception:  # a bad event must not take the model down
            logger.exception("personality model failed on %s", event.name)

    def _fold(self, event: RobotEvent) -> None:
        if isinstance(event, ActionFinished):
            self._on_action(event)
        elif isinstance(event, SensorUpdated):
            self._on_sensors(event)
        elif isinstance(event, BatteryUpdated):
            self._on_battery(event)
        elif isinstance(event, BehaviorCompleted):
            stimulus = self.stimulus_map.get(event.behavior)
            if stimulus is not None and event.outcome == "completed":
                self.record(stimulus)

    def _on_action(self, event: ActionFinished) -> None:
        action = event.action
        if action.status is ActionStatus.SUCCEEDED:
            self.record(Stimulus.SUCCESSFUL_MOVEMENT)
            return
        if action.rejection in HAZARD_REASONS:
            self.record(Stimulus.OBSTACLE)
            return
        if action.status in {ActionStatus.FAILED, ActionStatus.TIMED_OUT}:
            self.record(Stimulus.FAILED_MOVEMENT)

    def _on_sensors(self, event: SensorUpdated) -> None:
        sensors = event.sensors
        if sensors.touch_detected:
            self.record(Stimulus.TOUCH)
        # Edge-triggered: a cliff that stays asserted for ten frames is one surprise, not
        # ten. Without this the arousal variable would saturate on a single hazard.
        blocked = sensors.blocked
        if blocked and not self._hazard_asserted:
            self.record(Stimulus.OBSTACLE)
        self._hazard_asserted = blocked

    def _on_battery(self, event: BatteryUpdated) -> None:
        charging = event.battery.charging
        if charging is not self.engine.charging:
            self.engine.set_charging(charging, now=self._clock())
        if charging:
            self.record(Stimulus.CHARGING)

    def __repr__(self) -> str:
        return f"<PersonalityModel {self.robot_id} {self.traits.describe()}>"


def drives_snapshot(model: PersonalityModel) -> dict[str, Any]:
    """The current values as plain data, for an API response or a debug line."""
    return {"robot_id": model.robot_id, "traits": model.traits.model_dump(), "state": model.state.model_dump()}


__all__ = [
    "DEFAULT_BEHAVIOR_STIMULI",
    "HAZARD_REASONS",
    "PersonalityModel",
    "drives_snapshot",
]
