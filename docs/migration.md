# Migration record: inherited tree → Nilo

What changed when the repository stopped being a snapshot of `xiaozhi-esp32-server` and became
Nilo. The pre-migration tree is commit `ed27727` on `develop`.

The record covers two steps:

1. **Off the inherited tree.** The server directory, packaging, tooling and documentation were
   replaced, the management console and its clients were removed, and the device routes moved
   behind a protocol registry. A compatibility protocol kept devices running Xiaozhi-family
   firmware connecting on their original routes.
2. **Nilo, one protocol.** The product and backend became Nilo / `nilo-server` (environment
   prefix `NILO_`, images `ghcr.io/aa-box/nilo-server`, compose service `nilo-server`); the
   compatibility protocol and its `/xiaozhi/…` routes were deleted; the deprecated `xiaozhi:`
   config alias was removed; `protocols.strict` now defaults to `true`; the Chinese wake phrases
   were dropped. Devices flashed with Xiaozhi-family firmware can no longer connect — they must be
   reflashed against `/nilo/ota/`. There is no fallback and no compatibility mode.

The tables below describe the end state; where step 2 undid something step 1 had kept, the row
says so.

## Renamed

| Before | After | Notes |
|---|---|---|
| GitHub repository `AA-Box/Cozmo` | `AA-Box/Nilo` | renamed after the migration landed; GitHub redirects keep old clone URLs working |
| `main/xiaozhi-server/` | `main/nilo-server/` | `git mv`; Python imports are relative to the server directory, so no import changed |
| `.cozmo/sync-upstream.sh` | `scripts/sync-upstream.sh` | `.cozmo/` removed |
| config key `xiaozhi:` (server hello template) | `hello:` | step 1 kept the old key as a deprecated alias; step 2 removed it — only `hello:` loads |
| Docker `WORKDIR /opt/xiaozhi-esp32-server` | `/opt/nilo-server` | volume mounts in compose updated |
| images `ghcr.io/xinnan-tech/xiaozhi-esp32-server:{server-base,server_latest,web_latest}` | `ghcr.io/aa-box/nilo-server:{base,latest,X.Y.Z}` | web image dropped |
| compose service `xiaozhi-esp32-server` | `nilo-server` | `docker-compose_all.yml` (MySQL/Redis/console stack) deleted |
| MCP `clientInfo.name` `XiaozhiClient` / `XiaozhiMCPEndpointClient` | `nilo-server` | sent in `initialize`; not interpreted by firmware |
| log version `0.9.6` (upstream) | `robot.__version__` = `0.1.0` | `config/logger.py` |
| `Dockerfile-server-base` base `python:3.10-slim`, Aliyun pip mirror, `zh_CN` locale | `python:3.12-slim`, default index, `C.UTF-8` | |
| CI jobs `Lint (xiaozhi-server)`, `Python (xiaozhi-server)`, `Python 3.12 (xiaozhi-server, dev deps only)`, `Java (manager-api)`, `Vue (manager-web)` | `Lint and type-check`, `Python 3.12, full dependencies`, `Python 3.12, dev dependencies only`, `Docker Compose validates` | |
| default LLM persona (`小智`, Taiwanese girl) | Nilo robot persona, English | `config.yaml` `prompt:` and `agent-base-prompt.txt` |

## Deleted

Components (see "Components removed" below for why):
`main/manager-api/` (488 files), `main/manager-web/` (520), `main/manager-mobile/` (169),
`main/digital-human/` (99).

Root: `Dockerfile-web`, `docker-setup.sh` (Chinese whiptail installer for the full stack),
`docs/docker/{nginx.conf,start.sh}`, `.trae/`, `.cozmo/unmerged-prs/` (25 archived upstream
patches plus their index), `main/README.md`, `main/README_en.md`.

Server: `config_from_api.yaml`, `docker-compose_all.yml`, `test_plugin.py`,
`plugins/preprocess_plugin/` (Chinese-template smart-home intent rules), `music/*.mp3`
(3 sample songs), `plugins_func/functions/call_device.py` (called the removed console),
`CLAUDE.md` (rewritten).

Protocol (step 2): `robot/protocol/legacy_xiaozhi.py`, the compatibility protocol definition, and
with it the `/xiaozhi/v1/` and `/xiaozhi/ota/` routes. `RESERVED_MESSAGE_TYPES` moved into
`robot/protocol/nilo.py`, which is now the only protocol module.

Documentation: 32 inherited guides under `docs/`, `docs/readme/README_{en,de,vi,pt_BR}.md`,
56 images under `docs/images/`, `docs/upstream-strategy.md` (folded into
[upstream.md](upstream.md)). Code-verified facts were extracted before deletion and used to write
the new tree. The old root `README.md` was replaced.

GitHub: four Chinese issue templates replaced by English `bug_report`, `feature_request`,
`documentation` templates plus a pull-request template.

## Added

* `main/nilo-server/robot/` — Nilo-owned package. `robot/protocol/` holds `ProtocolSpec`,
  `ProtocolRegistry` and the single device protocol, `nilo` (`ALL_PROTOCOLS == (NILO,)`);
  `robot/protocol/nilo.py` also owns `RESERVED_MESSAGE_TYPES`.
* `config/placeholders.py` — `is_placeholder()`; shipped placeholders are `<your-...>`.
* `NILO_CONFIG`, `NILO_SERVER_HOST`, `NILO_SERVER_PORT`, `NILO_HTTP_PORT`, `NILO_LOG_LEVEL`
  (`config/config_loader.py`).
* `protocols:` config block (`nilo.enabled`, `strict`).
* Tests: `tests/robot/test_protocol.py`, `tests/config/test_nilo_config.py`,
  `tests/core/test_http_routes.py`, `tests/core/test_ws_path_gate.py`, `tests/test_compose.py`,
  `tests/test_imports.py`; `tests/fixtures/test_config.yaml` + `NILO_CONFIG` in
  `tests/conftest.py` (two module-level skips removed). Step 2 rewrote the first five; several now
  assert that the retired routes and the removed config alias stay gone.
* `scripts/smoke_check.py` — exercises a running server on the `nilo` routes and separately probes
  the retired routes, expecting 404.
* `scripts/check_docs.py` — mechanical check of links, paths, `NILO_*` names and the branding rules
  (run in CI as **Documentation and branding check**).
* This documentation tree (`docs/*.md`), `main/nilo-server/CLAUDE.md`.

## Public endpoints

| Route | Status |
|---|---|
| `ws://host:8000/nilo/v1/` | the only WebSocket route; advertised by OTA |
| `http://host:8003/nilo/ota/`, `/nilo/ota/download/{filename}` | the only OTA routes |
| `ws://host:8000/xiaozhi/v1/` | **removed** — not served; the handshake is rejected with 404 |
| `http://host:8003/xiaozhi/ota/`, `/xiaozhi/ota/download/{filename}` | **removed** — not registered; 404 |
| `http://host:8003/mcp/vision/explain` | unchanged |
| any other WebSocket path | rejected with 404 (`protocols.strict`, shipped `true`); set it `false` to restore the inherited accept-anything behaviour |

A device flashed with Xiaozhi-family firmware can no longer reach this server on any route: the
protocol that served those paths is gone, and `strict` rejects the path rather than falling through
to the session handler. Reflash the device against `/nilo/ota/` (or point it at `/nilo/v1/`
directly). Regression tests keep this true — `tests/core/test_ws_path_gate.py` (404 on
`/xiaozhi/v1/`), `tests/core/test_http_routes.py` (no `/xiaozhi/` route is registered) and
`tests/robot/test_protocol.py` — and `scripts/smoke_check.py` probes both retired routes.

The OTA response advertises `/nilo/v1/` by default; set `server.websocket` to advertise
something else (e.g. behind TLS).

## Configuration migration

| Old | New | Behaviour |
|---|---|---|
| `xiaozhi:` | `hello:` | alias removed in step 2: `config_loader.DEPRECATED_KEYS` is empty, so `xiaozhi:` is simply an unknown key and is ignored. Rename it |
| placeholder values containing `你`/`你的` | `<your-...>` | both markers recognised by `is_placeholder()`; user files with old placeholders keep working |
| (none) | `protocols:` | absent → the same as shipped: `nilo` enabled, `strict` on |
| `exit_commands: [退出, 关闭]` | `exit, quit, goodbye` added | old phrases kept |
| `wakeup_words` (Chinese phrases) | `hey nilo`, `hi nilo`, `hello nilo` | step 2 dropped the Chinese phrases with legacy firmware support; the list is English only |
| `system_error_response` (Chinese) | English | |
| `plugins.get_weather.api_key` (a shared real key) | `<your-qweather-api-key>` | the shared key is no longer committed |
| fish-speech `reference_text` (Chinese transcript) | `<transcript of your reference audio, word for word>` | a placeholder now; it must match your own `reference_audio` exactly |
| `manager-api:` block, `read_config_from_api` | no longer shipped in `config.yaml`, **deprecated** | the code still reads them from a user file; remote-config mode against the removed console; no server ships with Nilo |

Environment variables win over both config files.

## Compatibility retained

Step 2 removed nearly all of it. What is left:

* `你` placeholder marker (`config/placeholders.py`) — old user files keep working
* the Chinese `exit_commands` phrases, alongside the English ones
* `manager-api` / `read_config_from_api` code paths (`config/manage_api_client.py`,
  `core/handle/reportHandle.py`, branches in `core/connection.py`, `core/api/vision_handler.py`,
  `core/http_server.py`) — dormant unless `manager-api.url` is set
* `[device_call]` prefix handling in `core/handle/textHandler/listenMessageHandler.py` (device-to-device
  call feature that needed the console; harmless without it)

Gone, with nothing in their place:

* the `xiaozhi:` config key alias — rename the key to `hello:`
* the `/xiaozhi/v1/` and `/xiaozhi/ota/` routes and the protocol behind them — reflash the device
* the permissive WebSocket path gate — it now rejects unknown paths unless `protocols.strict: false`

## Remaining legacy identifiers and why

| Where | Why it stays |
|---|---|
| `scripts/sync-upstream.sh` `UPSTREAM_URL` | points at the upstream repository |
| `LICENSE` `Copyright (c) 2025 xinnan-tech`, and the attribution sentence in `README.md` | required MIT attribution |
| `tests/core/test_ws_path_gate.py`, `tests/core/test_http_routes.py`, `tests/robot/test_protocol.py`, `tests/config/test_nilo_config.py`, `tests/test_compose.py`, `scripts/smoke_check.py` | they name the retired routes and the removed alias in order to assert they stay retired |
| `scripts/check_docs.py` | holds the allow-list of files permitted to contain the word |
| `config.yaml` `exit_commands` `退出`, `关闭` | functional data matched against what the user says |
| `core/utils/textUtils.py` Chinese punctuation table; `cnlunar` in `core/utils/current_time.py`, the `get_lunar` tool in `plugins_func/functions/get_time.py` | functional text processing / lunar-calendar feature |
| provider adapters for Chinese vendors (Aliyun, Doubao, Xunfei, Tencent, Baidu, ChatGLM, Coze, Dify…) | working integrations; documented as region-specific in [providers.md](providers.md) |
| `docs/upstream.md`, [branding.md](branding.md), this file | provenance, the branding rules themselves, and the migration record |

`Cozmo` is named in the README only as design inspiration. The Chinese text left in the tree is functional data — the values above, the
categories and source names the news and weather plugins send to Chinese services
(`plugins_func/functions/get_news_from_chinanews.py`, `get_news_from_newsnow.py`,
`get_weather.py`), the `你的` placeholder marker the `performance_tester/` scripts check for, and
sample strings in provider docstrings — plus the tests of Chinese text handling
(`tests/core/utils/test_textUtils.py`, the lunar-date assertions in
`tests/core/utils/test_current_time.py`).

## Components removed and why

| Component | Verdict | Reasons |
|---|---|---|
| `manager-api` (Spring Boot, Java package `xiaozhi.*`, MySQL + Redis + Liquibase) | removed | Xiaozhi assistant admin console (agents, prompt templates, voice clones, SMS sign-up, knowledge bases, device address book). Robot management for Nilo is planned inside the Python backend ([robot-roadmap.md](robot-roadmap.md) Phase 7). 411 of 488 files contained Chinese; rebranding meant renaming every Java package. The Python server's dependency on it is optional (`manager-api.url` unset → local config). |
| `manager-web` (Vue 2) | removed | UI for manager-api only |
| `manager-mobile` (uni-app) | removed | Wi-Fi provisioning + agent config against manager-api only |
| `digital-human` (browser client + Chinese wake-word runtime) | removed | a Xiaozhi test client, not backend; Nilo's simulator is planned in Python (`robot/simulator/`) |
| `plugins/preprocess_plugin` | removed | smart-home voice-command matching on Chinese templates (`intents.yaml`) |
| `plugins_func/functions/call_device.py` | removed | called `manager-api` endpoints |

Everything removed is recoverable from git history (`git show ed27727:main/manager-api/...`).

## Database

Nilo Server has no database. The removed console owned the MySQL schema (`ai_*`, `sys_*` tables);
no migration debt remains in this repository.

## Pending admin actions (not done by the migration)

1. ~~Enable GitHub Actions for the repository.~~ Done: the Tests workflow runs on every pull request.
2. Publish the first base image: run the **Build Base Image** workflow (`workflow_dispatch`) so `Dockerfile-server` can pull `ghcr.io/aa-box/nilo-server:base`; until then build both images locally with `make docker-build`.
