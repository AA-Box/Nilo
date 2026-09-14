# Contributing to Nilo

Nilo is an open backend platform for autonomous social robots. This page is the short version;
[`docs/development.md`](docs/development.md) has the full developer guide.

## Setup

```bash
git clone https://github.com/AA-Box/Nilo.git nilo
cd nilo/main/nilo-server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # lint, types, tests
pip install -r requirements.txt          # add this to run the server
```

`requirements-dev.txt` alone is enough to run the test suite: tests that need the full runtime
(torch, aiohttp, websockets, opuslib) skip themselves.

## Before you open a pull request

Run what CI runs, from the repository root:

```bash
make lint          # ruff
make typecheck     # mypy, strict for robot/
make check-docs    # links, paths, env vars, branding rules
make test          # pytest
```

CI runs the same four plus `docker compose config` and a second test job against the dev-only
dependency slice. The pull-request template lists the checklist.

## Where code goes

| You are changing | Put it in |
|---|---|
| Robot behaviour, actions, world state, protocol | `main/nilo-server/robot/` |
| A new AI provider | `main/nilo-server/core/providers/<kind>/<name>.py` + a config block whose `type` matches the filename |
| A tool the LLM can call | `main/nilo-server/plugins_func/functions/` with `@register_function` |
| Anything else | see [`docs/development.md`](docs/development.md) |

`main/nilo-server/core/`, `config/`, `plugins_func/` and `models/` are derived from an upstream
project ([`docs/upstream.md`](docs/upstream.md)). Every line changed there is a line that can
conflict when upstream work is ported, so prefer a new file under `robot/` and attach to `core/`
through the smallest possible hook.

## Rules that reviews enforce

* **The LLM never controls motors.** It requests semantic actions (`move`, `turn`, `look_at`,
  `follow`, `play_animation`, `stop`) with bounded parameters; the device executes trajectories and
  owns acceleration limits, collision and cliff avoidance, watchdogs and emergency stop. See
  [`docs/safety-model.md`](docs/safety-model.md).
* **English only** in code, comments, log lines, and documentation. Existing Chinese text is
  functional data and is documented in [`docs/migration.md`](docs/migration.md).
* **No product name in domain code.** `RobotSession`, not `NiloRobotSession`. Kivo-era and upstream
  names must not come back; `make check-docs` fails if they do. See
  [`docs/branding.md`](docs/branding.md).
* **Documentation stays true.** If you change a route, config key, command or default, update the
  page that documents it. Label anything not yet built **Planned**.

## Commits and pull requests

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`; `!` for a breaking change).
Explain *why* in the body when it is not obvious. Branch off `develop` and target `develop`.

## Reporting problems

Bugs and feature requests go to [issues](https://github.com/AA-Box/Nilo/issues) using the
templates. For anything security-sensitive, read [`SECURITY.md`](SECURITY.md) first.
