# nilo-server — notes for coding agents

This directory is the Nilo backend (Python 3.12, asyncio). The human-facing docs are the
source of truth; read them before touching anything:

* [`docs/architecture.md`](../../docs/architecture.md) — how the server works today
* [`docs/robot-architecture.md`](../../docs/robot-architecture.md) — where robot code goes and the safety rule
* [`docs/development.md`](../../docs/development.md) — setup, lint, tests, layout
* [`docs/upstream.md`](../../docs/upstream.md) — which code is inherited and how it is synced
* [`docs/branding.md`](../../docs/branding.md) — naming rules (no product name in domain code, no Chinese)

## Layout in one glance

```
app.py            entry point
config/           YAML layering, NILO_* env overrides, placeholders, logging   (inherited, edited)
core/             session server, handlers, providers, tool system            (inherited — minimise edits)
plugins*/         interceptor plugins and @register_function tools             (inherited)
robot/            Nilo-owned code; robot/protocol holds the nilo + legacy route registry
tests/            pytest; tests/conftest.py points NILO_CONFIG at tests/fixtures/test_config.yaml
```

Rules of thumb:

* New robot functionality goes under `robot/`; attach to `core/` through the smallest possible
  hook. Every line changed in `core/` is a line that conflicts on the next upstream port.
* The LLM never gets a tool that sets motor/servo/PWM values. Semantic actions only
  (`docs/robot-architecture.md`, `docs/safety.md`).
* Device routes live only in `robot/protocol/`; never spell a path into `core/`.
* English only in comments, log lines and docs.

## Commands that work

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt   # full; on macOS vosk is skipped automatically
pytest -q                 # 124 tests with full deps; the dev slice (requirements-dev.txt only) skips heavy ones
ruff check .
mypy                      # robot/ only, strict
python app.py             # needs data/.config.yaml or NILO_CONFIG=<file>; see docs/getting-started.md
python ../../scripts/smoke_check.py   # against a running server
```
