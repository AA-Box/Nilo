# nilo-server — notes for coding agents

This directory is the Nilo backend (Python 3.12, asyncio). The human-facing docs are the
source of truth; read them before touching anything:

* [`docs/architecture.md`](../../docs/architecture.md) — how the server works today
* [`docs/robot-architecture.md`](../../docs/robot-architecture.md) — where robot code goes and the safety rule
* [`docs/robot-domain.md`](../../docs/robot-domain.md) — the robot domain layer that is implemented
* [`docs/robot-actions.md`](../../docs/robot-actions.md) — the action and safety layers, and how to use them
* [`docs/safety.md`](../../docs/safety.md) — the safety split, and what firmware must implement itself
* [`docs/robot-simulator.md`](../../docs/robot-simulator.md) — the simulator: run it, extend it, test against it
* [`docs/development.md`](../../docs/development.md) — setup, lint, tests, layout
* [`docs/upstream.md`](../../docs/upstream.md) — which code is inherited and how it is synced
* [`docs/branding.md`](../../docs/branding.md) — naming rules (no product name in domain code, no Chinese)

## Layout in one glance

```
app.py            entry point
config/           YAML layering, NILO_* env overrides, placeholders, logging   (inherited, edited)
core/             session server, handlers, providers, tool system            (inherited — minimise edits)
plugins*/         interceptor plugins and @register_function tools             (inherited)
robot/            Nilo-owned code: protocol/ (routes), state/ (models + store + the action
                  vocabulary + the world model), events/ (bus), devices/ (robot registry,
                  MCP capability discovery), safety/ (limits, deterministic policy, e-stop,
                  watchdog), actions/ (the ten semantic actions, queue, registry, executor),
                  behavior/ (the utility-scored autonomy engine: tuning, scheduler, the
                  sixteen built-in behaviours, the explain CLI), personality/ (traits, the
                  internal control variables, the per-robot store), animation/ (YAML
                  animations and the engine that plays them), telemetry.py (device
                  notifications -> world state), simulator/ (a fake robot on a real socket),
                  runtime.py, session.py
tests/            pytest; tests/conftest.py points NILO_CONFIG at tests/fixtures/test_config.yaml.
                  tests/robot/ never opens a socket; tests/integration/ starts a real server
```

Rules of thumb:

* New robot functionality goes under `robot/`; attach to `core/` through the smallest possible
  hook. Every line changed in `core/` is a line that conflicts on the next upstream port.
* The LLM never gets a tool that sets motor/servo/PWM values. Semantic actions only
  (`docs/robot-architecture.md`, `docs/safety.md`).
* Nothing talks to a device except `robot/actions/executor.py`. New capabilities are new
  action specs, not new call sites.
* `robot/safety/` may import `robot/state/` and nothing else from the subsystem, and it
  rejects rather than clamps. `tests/robot/test_layering.py` enforces the first;
  `tests/robot/test_safety.py` the second.
* The behaviour engine never consults an LLM, never reads the wall clock, and never writes
  a number into a scoring function — tuning lives in `robot/behavior/tuning.py` and a test
  parses the source to prove it (`docs/robot-behavior.md`).
* Animations are YAML data under `robot/animation/library/`; adding one must never require a
  Python change (`docs/robot-animation.md`). Personality biases which action is proposed and
  can never reach safety — say "internal control variables", never "emotions"
  (`docs/robot-personality.md`).
* Backend safety is a policy filter, never a guarantee. Do not write a comment, log line or
  doc sentence implying the backend can stop a robot.
* Device routes live only in `robot/protocol/`; never spell a path into `core/`.
* English only in comments, log lines and docs.

## Commands that work

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt   # full; on macOS vosk is skipped automatically
pytest -q                 # whole suite; the dev slice (requirements-dev.txt only) skips the socket tests
ruff check .
mypy                      # robot/ only, strict
python app.py             # needs data/.config.yaml or NILO_CONFIG=<file>; see docs/getting-started.md
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --scenario person_enters_room
python -m robot.simulator --status    # the simulated robot's own state
python -m robot.behavior explain --situation person_arrives   # why would it do that?
python ../../scripts/smoke_check.py   # against a running server
```
