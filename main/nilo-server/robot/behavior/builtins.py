"""The starter behaviour set: sixteen behaviours the robot ships with.

Every number in this file comes from :class:`~robot.behavior.tuning.BehaviorTuning`.
There is no bare float in a scoring function — that is the rule the tuning module exists
to enforce, and ``tests/robot/test_behavior_tuning.py`` checks the source for it.

The bands, and why each behaviour is in the one it is:

    CRITICAL  go_to_charger, charging        power. Nothing social outranks a dying robot.
    HIGH      low_battery, wake, sleep,      system and reflexes. A robot that ignores
              react_to_touch                 being touched reads as broken.
    NORMAL    greet, look_at_person,         social. The everyday stuff.
              approach, follow, react_to_sound
    LOW       investigate, look_around,      curiosity and filler.
              explore, bored
    IDLE      idle                           the floor: what is left when nothing is worth doing.

Safety is not in this table. It is a layer below, it cannot be scored against, and the
highest-scoring behaviour here still gets its move rejected on a cliff (docs/safety.md).
"""

from __future__ import annotations

from typing import Any

from robot.behavior.base import (
    AutonomyMode,
    Behavior,
    BehaviorCategory,
    BehaviorContext,
    BehaviorPriority,
    BehaviorResult,
)
from robot.behavior.tracking import decide_follow, decide_look_at
from robot.behavior.tuning import BehaviorTuning
from robot.state.actions import Resource
from robot.state.world import Entity, EntityType

#: Memory keys. Namespaced strings rather than attributes, because the memory outlives
#: any one behaviour instance and is keyed by more than the behaviour name.
ASLEEP = "state:asleep"
GREETED = "greeted:"
LOOKED_AT = "looked_at:"
FOLLOWED = "followed:"
INVESTIGATED = "investigated:"
VISITED = "visited:"


def _fresh_people(context: BehaviorContext) -> tuple[Entity, ...]:
    """People seen recently enough to act on, as of the snapshot being scored."""
    return context.world.seen_within(
        EntityType.PERSON, context.tuning.person_fresh_s, context.world.updated_at
    )


def _charger(context: BehaviorContext) -> Entity | None:
    """The dock, if the robot knows where one is."""
    for location in context.world.locations:
        if str(location.attributes.get("label", "")) in {"charger", "dock"}:
            return location
    return None


def _asleep(context: BehaviorContext) -> bool:
    return context.memory.at(ASLEEP) is not None


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _ramp(value: float, start: float, full: float) -> float:
    """0 below ``start``, 1 at ``full``, linear between. The shape every "rises over time"
    score in this file uses, written once."""
    if full <= start:
        return 1.0 if value >= full else 0.0
    return _clamp((value - start) / (full - start))


# -- power ------------------------------------------------------------------------------------


class GoToChargerBehavior(Behavior):
    """Drive to the dock. The one behaviour that is allowed to interrupt anything."""

    name = "go_to_charger"
    category = BehaviorCategory.SAFETY
    priority = BehaviorPriority.CRITICAL
    required_resources = frozenset({Resource.DRIVE})
    cooldown_s = 0.0
    interruptible = False

    def can_run(self, context: BehaviorContext) -> bool:
        battery = context.world.battery_percent
        if battery is None or context.world.charging:
            return False
        if battery > context.tuning.battery_low_percent:
            return False
        return _charger(context) is not None

    def score(self, context: BehaviorContext) -> float:
        battery = context.world.battery_percent
        if battery is None:
            return 0.0
        tuning = context.tuning
        if battery <= tuning.battery_critical_percent:
            context.because(f"battery at {battery}% is below the critical line")
            return tuning.go_to_charger_critical_score
        # Between the low and the critical line the urgency ramps: at the low threshold it
        # is worth doing, at the critical one it is worth doing instead of anything else.
        span = max(1, tuning.battery_low_percent - tuning.battery_critical_percent)
        urgency = (tuning.battery_low_percent - battery) / span
        context.because(f"battery at {battery}% is below the low threshold")
        return _clamp(
            tuning.go_to_charger_score
            + urgency * (tuning.go_to_charger_critical_score - tuning.go_to_charger_score)
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        dock = _charger(context)
        if dock is None:  # pragma: no cover - can_run already refused this
            return BehaviorResult.skipped("no charger is known")
        record = None
        if dock.position is not None and abs(dock.position.bearing_deg) > 1:
            record = await context.robot.turn(angle_deg=int(dock.position.bearing_deg))
        step = await context.robot.move(distance_mm=context.tuning.go_to_charger_step_mm)
        return BehaviorResult.completed(f"driving to {dock.id}", record, step)


class ChargingBehavior(Behavior):
    """Sit on the dock and look content. Suppresses everything else while it charges."""

    name = "charging"
    category = BehaviorCategory.SYSTEM
    priority = BehaviorPriority.CRITICAL
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE
    cooldown_s = 0.0

    def can_run(self, context: BehaviorContext) -> bool:
        battery = context.world.battery_percent
        return context.world.charging and (
            battery is None or battery < context.tuning.charged_enough_percent
        )

    def score(self, context: BehaviorContext) -> float:
        context.because("on the charger")
        return context.tuning.charging_score

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        record = await context.robot.set_expression("sleepy", intensity_pct=40)
        return BehaviorResult.completed("charging", record)


class LowBatteryBehavior(Behavior):
    """Say it is low. Runs when docking cannot — no dock known, or PASSIVE mode."""

    name = "low_battery"
    category = BehaviorCategory.SYSTEM
    priority = BehaviorPriority.HIGH
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE
    cooldown_s = 30.0

    def can_run(self, context: BehaviorContext) -> bool:
        battery = context.world.battery_percent
        return (
            battery is not None
            and not context.world.charging
            and battery <= context.tuning.battery_low_percent
        )

    def score(self, context: BehaviorContext) -> float:
        battery = context.world.battery_percent or 0
        context.because(f"battery at {battery}% with no dock in reach")
        return context.tuning.low_battery_score

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        record = await context.robot.set_expression("sad", intensity_pct=60)
        return BehaviorResult.completed("low battery shown", record)


# -- sleep and wake ---------------------------------------------------------------------------------


class SleepBehavior(Behavior):
    """Go quiet after a long time with nothing happening."""

    name = "sleep"
    category = BehaviorCategory.SYSTEM
    priority = BehaviorPriority.HIGH
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE

    def can_run(self, context: BehaviorContext) -> bool:
        if _asleep(context) or _fresh_people(context):
            return False
        return context.idle_s() >= context.tuning.sleep_after_idle_s

    def score(self, context: BehaviorContext) -> float:
        context.because(f"nothing has happened for {context.idle_s():.0f}s")
        return context.tuning.sleep_score

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        context.memory.mark(ASLEEP, context.now)
        record = await context.robot.set_expression("sleepy", intensity_pct=80)
        return BehaviorResult.completed("asleep", record)


class WakeBehavior(Behavior):
    """Come back. Outranks sleep, so a stimulus always wins over staying asleep."""

    name = "wake"
    category = BehaviorCategory.SYSTEM
    priority = BehaviorPriority.HIGH
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE

    def can_run(self, context: BehaviorContext) -> bool:
        if not _asleep(context):
            return False
        return bool(_fresh_people(context)) or context.since_stimulus() <= context.tuning.stimulus_fresh_s

    def score(self, context: BehaviorContext) -> float:
        context.because("something happened while asleep")
        return context.tuning.wake_score

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        context.memory.forget(ASLEEP)
        record = await context.robot.set_expression("surprised", intensity_pct=70)
        return BehaviorResult.completed("awake", record)


# -- reactions -----------------------------------------------------------------------------------------


class ReactToTouchBehavior(Behavior):
    """Somebody touched the robot. The most reliable social signal it has."""

    name = "react_to_touch"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.HIGH
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.touch_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        return context.world.touched

    def score(self, context: BehaviorContext) -> float:
        context.because("touch sensor asserted")
        tuning = context.tuning
        return _clamp(tuning.touch_score + tuning.touch_social_weight * context.drives.social_need)

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        record = await context.robot.set_expression("happy", intensity_pct=90)
        animation = await context.robot.play_animation("happy_wiggle")
        return BehaviorResult.completed("reacted to touch", record, animation)


class ReactToSoundBehavior(Behavior):
    """Something was loud. Turn towards it if the direction is known."""

    name = "react_to_sound"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.HEAD, Resource.DISPLAY})

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.sound_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        environment = context.world.environment
        if environment.sound_level < context.tuning.sound_threshold:
            return False
        return environment.age_seconds(context.world.updated_at) <= context.tuning.stimulus_fresh_s

    def score(self, context: BehaviorContext) -> float:
        level = context.world.environment.sound_level
        context.because(f"sound level {level:.2f} above the reaction threshold")
        tuning = context.tuning
        return _clamp(tuning.sound_score * level + tuning.sound_arousal_weight * context.drives.arousal)

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        direction = context.world.environment.sound_direction_deg
        record = await context.robot.set_expression("surprised", intensity_pct=60)
        if direction is None:
            return BehaviorResult.completed("reacted to a sound of unknown direction", record)
        head = await context.robot.head_angle(yaw_deg=int(direction))
        return BehaviorResult.completed(f"turned towards {direction:.0f} deg", record, head)


# -- social --------------------------------------------------------------------------------------------------


class GreetPersonBehavior(Behavior):
    """Say hello to somebody who has just turned up and has not been greeted lately."""

    name = "greet_person"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.DISPLAY, Resource.AUDIO})
    min_autonomy = AutonomyMode.PASSIVE

    def _target(self, context: BehaviorContext) -> Entity | None:
        """The freshest person nobody has greeted inside the cooldown."""
        for candidate in _fresh_people(context):
            if context.memory.since(GREETED + candidate.id, context.now) >= context.tuning.greet_cooldown_s:
                return candidate
        return None

    def can_run(self, context: BehaviorContext) -> bool:
        return self._target(context) is not None

    def score(self, context: BehaviorContext) -> float:
        target = self._target(context)
        if target is None:  # pragma: no cover - can_run already refused
            return 0.0
        tuning = context.tuning
        score = tuning.greet_score
        context.because(f"person {target.id} newly detected")
        if bool(target.attributes.get("known")):
            score += tuning.greet_known_bonus
            context.because("familiar person")
        if context.drives.social_need > 0.5:
            context.because("social drive high")
        score += tuning.sociability_weight * (context.drives.social_need - 0.5)
        context.because("greet cooldown expired")
        return _clamp(score * target.confidence)

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        target = self._target(context)
        if target is None:
            return BehaviorResult.skipped("the person went away before the greeting started")
        context.memory.mark(GREETED + target.id, context.now)
        expression = await context.robot.set_expression("happy", intensity_pct=90)
        animation = await context.robot.play_animation("excited_greeting")
        name = str(target.attributes.get("name", target.id))
        return BehaviorResult.completed(f"greeted {name}", expression, animation)


class LookAtPersonBehavior(Behavior):
    """Keep a person framed. Head only — this is not following."""

    name = "look_at_person"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.HEAD})

    def _target(self, context: BehaviorContext) -> Entity | None:
        attention = context.world.attention
        if attention is not None and attention.type is EntityType.PERSON:
            return attention
        people = _fresh_people(context)
        return people[0] if people else None

    def can_run(self, context: BehaviorContext) -> bool:
        target = self._target(context)
        if target is None or target.image_point is None:
            return False
        dx, dy = target.image_point.offset_from_centre()
        return max(abs(dx), abs(dy)) > context.tuning.look_at_deadband

    def score(self, context: BehaviorContext) -> float:
        target = self._target(context)
        if target is None or target.image_point is None:  # pragma: no cover - filtered
            return 0.0
        dx, dy = target.image_point.offset_from_centre()
        error = max(abs(dx), abs(dy))
        context.because(f"person {target.id} is {error:.2f} off centre")
        return _clamp(
            context.tuning.look_at_person_score
            + context.tuning.sociability_weight * (context.drives.social_need - 0.5)
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        target = self._target(context)
        if target is None or target.image_point is None:
            return BehaviorResult.skipped("the person is no longer visible")
        # Rate-limited and proportional (robot/behavior/tracking.py): perception can
        # report far faster than a head can move, and commanding the whole error makes it
        # oscillate.
        decision = decide_look_at(
            target.image_point,
            now=context.now,
            last_command_at=context.memory.at(LOOKED_AT + target.id),
            tuning=context.tuning,
        )
        if not decision:
            return BehaviorResult.skipped(decision.reason)
        context.memory.mark(LOOKED_AT + target.id, context.now)
        record = await context.robot.look_at(x_pct=decision.x_pct, y_pct=decision.y_pct)
        return BehaviorResult.completed(f"looking at {target.id}: {decision.reason}", record)


class ApproachPersonBehavior(Behavior):
    """Close the distance to somebody, one short leg at a time."""

    name = "approach_person"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.DRIVE})
    min_autonomy = AutonomyMode.FULL

    def _target(self, context: BehaviorContext) -> Entity | None:
        for candidate in _fresh_people(context):
            position = candidate.position
            if position is not None and position.distance_mm > context.tuning.approach_min_distance_mm:
                return candidate
        return None

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.approach_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        return not context.world.blocked and self._target(context) is not None

    def score(self, context: BehaviorContext) -> float:
        target = self._target(context)
        if target is None or target.position is None:  # pragma: no cover - filtered
            return 0.0
        context.because(f"person {target.id} is {target.position.distance_mm}mm away")
        return _clamp(
            context.tuning.approach_score
            + context.tuning.sociability_weight * (context.drives.social_need - 0.5)
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        target = self._target(context)
        if target is None or target.position is None:
            return BehaviorResult.skipped("the person moved out of reach")
        turn = None
        if abs(target.position.bearing_deg) > 5:
            turn = await context.robot.turn(angle_deg=int(target.position.bearing_deg))
        step = min(
            context.tuning.approach_step_mm,
            max(0, target.position.distance_mm - context.tuning.approach_min_distance_mm),
        )
        record = await context.robot.move(distance_mm=step)
        return BehaviorResult.completed(f"approached {target.id}", turn, record)


class FollowPersonBehavior(Behavior):
    """Keep a tracked person in front of the robot.

    Two paths, and the choice is made on what the world actually knows. With an
    ``image_point`` — a target vision is tracking — the loop is closed here, deterministically
    and one bounded leg at a time (``robot/behavior/tracking.py``), which is what makes
    following testable without a device. Without one, the target id is handed to the
    device's own follow tool, which closes the loop far faster than a round trip through
    this process can (docs/robot-actions.md).

    An LLM is not involved in either path.
    """

    name = "follow_person"
    category = BehaviorCategory.SOCIAL
    priority = BehaviorPriority.NORMAL
    required_resources = frozenset({Resource.DRIVE, Resource.HEAD})
    min_autonomy = AutonomyMode.FULL

    def _target(self, context: BehaviorContext) -> Entity | None:
        attention = context.world.attention
        if attention is not None and attention.type is EntityType.PERSON:
            return attention
        people = _fresh_people(context)
        return people[0] if people else None

    def can_run(self, context: BehaviorContext) -> bool:
        if context.world.blocked:
            return False
        target = self._target(context)
        return target is not None and target.position is not None

    def score(self, context: BehaviorContext) -> float:
        target = self._target(context)
        if target is None:  # pragma: no cover - filtered
            return 0.0
        context.because(f"following {target.id}")
        return _clamp(
            context.tuning.follow_score
            + context.tuning.sociability_weight * (context.drives.social_need - 0.5)
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        target = self._target(context)
        if target is None:
            return BehaviorResult.skipped("nobody to follow")
        if target.image_point is None:
            record = await context.robot.follow(
                target.id,
                duration_ms=context.tuning.follow_duration_ms,
                stop_distance_mm=context.tuning.follow_stop_distance_mm,
            )
            return BehaviorResult.completed(f"handed {target.id} to the device follow loop", record)

        offset_x, _ = target.image_point.offset_from_centre()
        decision = decide_follow(
            offset_x=offset_x,
            distance_mm=target.position.distance_mm if target.position else 0,
            now=context.now,
            last_command_at=context.memory.at(FOLLOWED + target.id),
            tuning=context.tuning,
            blocked=context.world.blocked,
        )
        if decision.stop:
            record = await context.robot.stop(decision.reason)
            return BehaviorResult.completed(f"stopped following {target.id}", record)
        if not decision.acts:
            return BehaviorResult.skipped(decision.reason)
        context.memory.mark(FOLLOWED + target.id, context.now)
        turn = await context.robot.turn(angle_deg=decision.turn_deg) if decision.turn_deg else None
        move = await context.robot.move(distance_mm=decision.move_mm) if decision.move_mm else None
        return BehaviorResult.completed(f"following {target.id}: {decision.reason}", turn, move)


# -- curiosity -----------------------------------------------------------------------------------------------------


class InvestigateObjectBehavior(Behavior):
    """Look at something new that is not a person."""

    name = "investigate_object"
    category = BehaviorCategory.EXPLORATION
    priority = BehaviorPriority.LOW
    required_resources = frozenset({Resource.HEAD})

    def _target(self, context: BehaviorContext) -> Entity | None:
        fresh = context.world.seen_within(
            EntityType.OBJECT, context.tuning.object_fresh_s, context.world.updated_at
        )
        for candidate in fresh:
            if context.memory.since(INVESTIGATED + candidate.id, context.now) >= context.tuning.investigate_cooldown_s:
                return candidate
        return None

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.investigate_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        return self._target(context) is not None

    def score(self, context: BehaviorContext) -> float:
        target = self._target(context)
        if target is None:  # pragma: no cover - filtered
            return 0.0
        label = str(target.attributes.get("label", "object"))
        context.because(f"unexamined {label} in view")
        return _clamp(
            context.tuning.investigate_score + context.tuning.curiosity_weight * context.drives.curiosity
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        target = self._target(context)
        if target is None:
            return BehaviorResult.skipped("the object is gone")
        context.memory.mark(INVESTIGATED + target.id, context.now)
        expression = await context.robot.set_expression("curious", intensity_pct=70)
        if target.image_point is not None:
            look = await context.robot.look_at(
                x_pct=int(target.image_point.x * 100), y_pct=int(target.image_point.y * 100)
            )
            return BehaviorResult.completed(f"investigated {target.id}", expression, look)
        return BehaviorResult.completed(f"investigated {target.id}", expression)


class LookAroundBehavior(Behavior):
    """Sweep the head. The cheapest way to learn something about the room."""

    name = "look_around"
    category = BehaviorCategory.EXPLORATION
    priority = BehaviorPriority.LOW
    required_resources = frozenset({Resource.HEAD})

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.look_around_cooldown_s

    def score(self, context: BehaviorContext) -> float:
        tuning = context.tuning
        if _fresh_people(context):
            context.because("somebody is here, so looking around is less interesting")
            return _clamp(tuning.look_around_base_score * tuning.look_around_busy_scale)
        context.because("nothing important is happening")
        return _clamp(
            tuning.look_around_base_score
            + tuning.look_around_curiosity_weight * context.drives.curiosity
            + tuning.look_around_boredom_weight * context.drives.boredom
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        sweep = context.rng.choice((-1, 1)) * context.tuning.explore_turn_deg
        left = await context.robot.head_angle(yaw_deg=sweep)
        centre = await context.robot.head_angle(yaw_deg=0)
        return BehaviorResult.completed("looked around", left, centre)


class ExploreBehavior(Behavior):
    """Drive somewhere else. The only behaviour that moves the robot for no reason."""

    name = "explore"
    category = BehaviorCategory.EXPLORATION
    priority = BehaviorPriority.LOW
    required_resources = frozenset({Resource.DRIVE})
    min_autonomy = AutonomyMode.FULL

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.explore_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        if context.world.blocked or context.world.charging:
            return False
        return not _fresh_people(context)

    def score(self, context: BehaviorContext) -> float:
        tuning = context.tuning
        context.because("curiosity high and nothing important happening")
        return _clamp(
            tuning.explore_base_score
            + tuning.curiosity_weight * context.drives.curiosity
            + tuning.explore_boredom_weight * context.drives.boredom
        )

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        angle = context.rng.choice((-1, 1)) * context.tuning.explore_turn_deg
        turn = await context.robot.turn(angle_deg=angle)
        step = await context.robot.move(distance_mm=context.tuning.explore_distance_mm)
        return BehaviorResult.completed("explored", turn, step)


class BoredBehavior(Behavior):
    """Show that nothing has happened for a long time. Scores higher the longer that is true."""

    name = "bored"
    category = BehaviorCategory.ENTERTAINMENT
    priority = BehaviorPriority.LOW
    required_resources = frozenset({Resource.DISPLAY})
    min_autonomy = AutonomyMode.PASSIVE

    def cooldown(self, tuning: BehaviorTuning) -> float:
        return tuning.boredom_cooldown_s

    def can_run(self, context: BehaviorContext) -> bool:
        return context.idle_s() >= context.tuning.boredom_onset_s

    def score(self, context: BehaviorContext) -> float:
        tuning = context.tuning
        idle = context.idle_s()
        ramp = _ramp(idle, tuning.boredom_onset_s, tuning.boredom_full_s)
        context.because(f"idle for {idle:.0f}s")
        return _clamp(tuning.boredom_max_score * max(ramp, context.drives.boredom))

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        expression = await context.robot.set_expression("sleepy", intensity_pct=30)
        animation = await context.robot.play_animation("bored_sigh")
        return BehaviorResult.completed("bored", expression, animation)


class IdleBehavior(Behavior):
    """The floor. Always eligible, always nearly worthless, never nothing."""

    name = "idle"
    category = BehaviorCategory.IDLE
    priority = BehaviorPriority.IDLE
    required_resources = frozenset()
    min_autonomy = AutonomyMode.PASSIVE
    min_runtime_s = 0.0

    def score(self, context: BehaviorContext) -> float:
        context.because("nothing else is worth doing")
        return context.tuning.idle_score

    async def execute(self, context: BehaviorContext) -> BehaviorResult:
        return BehaviorResult.completed("idle")


#: Every behaviour the robot ships with, in the order they are documented.
BUILTIN_BEHAVIORS: tuple[type[Behavior], ...] = (
    GoToChargerBehavior,
    ChargingBehavior,
    LowBatteryBehavior,
    SleepBehavior,
    WakeBehavior,
    ReactToTouchBehavior,
    ReactToSoundBehavior,
    GreetPersonBehavior,
    LookAtPersonBehavior,
    ApproachPersonBehavior,
    FollowPersonBehavior,
    InvestigateObjectBehavior,
    LookAroundBehavior,
    ExploreBehavior,
    BoredBehavior,
    IdleBehavior,
)


def default_behaviors(**overrides: Any) -> list[Behavior]:
    """One instance of every built-in behaviour, ready to register.

    ``overrides`` replaces a behaviour by name, so a deployment can swap one out without
    rebuilding the list: ``default_behaviors(explore=MyExplore())``.
    """
    instances: list[Behavior] = []
    for behavior_type in BUILTIN_BEHAVIORS:
        replacement = overrides.pop(behavior_type.name, None)
        instances.append(replacement if replacement is not None else behavior_type())
    if overrides:
        raise ValueError(f"no built-in behaviour named {', '.join(sorted(overrides))}")
    return instances


__all__ = [
    "ASLEEP",
    "BUILTIN_BEHAVIORS",
    "FOLLOWED",
    "GREETED",
    "LOOKED_AT",
    "INVESTIGATED",
    "ApproachPersonBehavior",
    "BoredBehavior",
    "ChargingBehavior",
    "ExploreBehavior",
    "FollowPersonBehavior",
    "GoToChargerBehavior",
    "GreetPersonBehavior",
    "IdleBehavior",
    "InvestigateObjectBehavior",
    "LookAroundBehavior",
    "LookAtPersonBehavior",
    "LowBatteryBehavior",
    "ReactToSoundBehavior",
    "ReactToTouchBehavior",
    "SleepBehavior",
    "WakeBehavior",
]
