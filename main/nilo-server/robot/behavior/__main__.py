"""``python -m robot.behavior`` — ask the engine why it would do what it does.

    python -m robot.behavior explain --situation person_arrives
    python -m robot.behavior explain --world snapshot.json --mode full --json
    python -m robot.behavior situations
    python -m robot.behavior behaviors

Scoring only: this command can never move a robot. It builds an engine over a world
snapshot with a handle that records instead of commanding, which is why it is safe to run
against production tuning on a laptop.

    $ python -m robot.behavior explain --situation person_arrives
    selected: greet_person
    score: 0.84

    alternatives:
      look_at_person: 0.45
      look_around: 0.09

    reasons:
      person person-1 newly detected
      familiar person
      greet cooldown expired
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from robot.behavior.base import AutonomyMode
from robot.behavior.builtins import BUILTIN_BEHAVIORS
from robot.behavior.explain import SITUATIONS, explain_world, get_situation, load_world
from robot.behavior.tuning import BehaviorTuning, load_tuning


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m robot.behavior",
        description="Explain the robot's behaviour decisions. Scores; never commands.",
    )
    sub = parser.add_subparsers(dest="command")

    explain = sub.add_parser("explain", help="why would the robot do what it does?")
    source = explain.add_mutually_exclusive_group()
    source.add_argument(
        "--situation",
        default="person_arrives",
        choices=sorted(SITUATIONS),
        help="a built-in world snapshot (default: person_arrives)",
    )
    source.add_argument("--world", help="a world snapshot as JSON, instead of a built-in situation")
    explain.add_argument(
        "--mode",
        default=AutonomyMode.NORMAL.value,
        choices=[mode.value for mode in AutonomyMode],
        help="autonomy mode to evaluate in (default: normal)",
    )
    explain.add_argument("--seed", type=int, default=0, help="RNG seed; the same seed gives the same answer")
    explain.add_argument(
        "--now",
        type=float,
        default=0.0,
        help="engine clock in seconds; cooldowns and the idle timer are measured from it",
    )
    explain.add_argument("--tuning", help="a behaviour tuning YAML file (default: built-in defaults)")
    explain.add_argument("--json", action="store_true", help="machine-readable output")
    explain.add_argument("--all", action="store_true", help="include every candidate, not just the top few")

    sub.add_parser("situations", help="list the built-in world snapshots")
    sub.add_parser("behaviors", help="list the registered behaviours and their bands")
    return parser


def _explain(args: argparse.Namespace) -> int:
    world = load_world(args.world) if args.world else get_situation(args.situation)
    tuning: BehaviorTuning = load_tuning(args.tuning) if args.tuning else BehaviorTuning()
    decision = explain_world(
        world,
        mode=AutonomyMode(args.mode),
        tuning=tuning,
        seed=args.seed,
        now=args.now,
    )
    if args.json:
        print(json.dumps(decision.as_dict(), indent=2, sort_keys=True))
        return 0
    limit = len(decision.candidates) if args.all else 3
    print(decision.explain(limit=limit), end="")
    return 0


def _situations() -> int:
    for name in sorted(SITUATIONS):
        doc = (SITUATIONS[name].__doc__ or "").strip().splitlines()
        print(f"{name:16} {doc[0] if doc else ''}")
    return 0


def _behaviors() -> int:
    print(f"{'name':22} {'category':14} {'band':9} {'autonomy':9} resources")
    for behavior_type in BUILTIN_BEHAVIORS:
        behavior = behavior_type()
        resources = ",".join(sorted(r.value for r in behavior.required_resources)) or "-"
        print(
            f"{behavior.name:22} {behavior.category.value:14} "
            f"{int(behavior.priority):<9} {behavior.min_autonomy.value:9} {resources}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "situations":
        return _situations()
    if args.command == "behaviors":
        return _behaviors()
    if args.command == "explain" or args.command is None:
        if args.command is None:  # bare invocation explains the default situation
            args = parser.parse_args(["explain", *(argv or [])])
        return _explain(args)
    parser.print_help()  # pragma: no cover - argparse handles unknown commands
    return 2


if __name__ == "__main__":
    sys.exit(main())
