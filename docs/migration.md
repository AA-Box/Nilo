# Migration record: inherited tree → Kivo

What changed when the repository stopped being a snapshot of `xiaozhi-esp32-server` and became
Kivo. Everything here is on the `kivo-migration` branch history; the pre-migration tree is
commit `ed27727` on `develop`.

## Renamed

| Before | After | Notes |
|---|---|---|
| `main/xiaozhi-server/` | `main/kivo-server/` | `git mv`; Python imports are relative to the server directory, so no import changed |
| `.cozmo/sync-upstream.sh` | `scripts/sync-upstream.sh` | `.cozmo/` removed |
| config key `xiaozhi:` (server hello template) | `hello:` | old key still loads with a deprecation warning, per config layer |
| Docker `WORKDIR /opt/xiaozhi-esp32-server` | `/opt/kivo-server` | volume mounts in compose updated |
| images `ghcr.io/xinnan-tech/xiaozhi-esp32-server:{server-base,server_latest,web_latest}` | `ghcr.io/aa-box/kivo-server:{base,latest,X.Y.Z}` | web image dropped |
| compose service `xiaozhi-esp32-server` | `kivo-server` | `docker-compose_all.yml` (MySQL/Redis/console stack) deleted |
| MCP `clientInfo.name` `XiaozhiClient` / `XiaozhiMCPEndpointClient` | `kivo-server` | sent in `initialize`; not interpreted by firmware |
| log version `0.9.6` (upstream) | `robot.__version__` = `0.1.0` | `config/logger.py` |
| `Dockerfile-server-base` base `python:3.10-slim`, Aliyun pip mirror, `zh_CN` locale | `python:3.12-slim`, default index, `C.UTF-8` | |
| CI jobs `Lint (xiaozhi-server)`, `Python (xiaozhi-server)`, `Java (manager-api)`, `Vue (manager-web)` | `Lint and type-check`, `Python 3.12, full dependencies`, `Python 3.12, dev dependencies only`, `Docker Compose validates` | |
| default LLM persona (小智, Taiwanese girl) | Kivo robot persona, English | `config.yaml` `prompt:` and `agent-base-prompt.txt` |

## Deleted

Components (see "Components removed" below for why):
`main/manager-api/` (488 files), `main/manager-web/` (520), `main/manager-mobile/` (169),
`main/digital-human/` (99).

Root: `Dockerfile-web`, `docker-setup.sh` (Chinese whiptail installer for the full stack),
`docs/docker/{nginx.conf,start.sh}`, `.trae/`, `.cozmo/unmerged-prs/` (26 archived upstream
patches), `main/README.md`, `main/README_en.md`.

Server: `config_from_api.yaml`, `docker-compose_all.yml`, `test_plugin.py`,
`plugins/preprocess_plugin/` (Chinese-template smart-home intent rules), `music/*.mp3`
(3 sample songs), `plugins_func/functions/call_device.py` (called the removed console),
`CLAUDE.md` (rewritten).

Documentation: 32 inherited guides under `docs/`, `docs/readme/README_{en,de,vi,pt_BR}.md`,
56 images under `docs/images/`, `docs/upstream-strategy.md` (folded into
[upstream.md](upstream.md)). Code-verified facts were extracted before deletion and used to write
the new tree. The old root `README.md` was replaced.

GitHub: four Chinese issue templates replaced by English `bug_report`, `feature_request`,
`documentation` templates plus a pull-request template.

## Added

* `main/kivo-server/robot/` — Kivo-owned package. `robot/protocol/` holds `ProtocolSpec`,
  `ProtocolRegistry`, the `kivo` and `legacy_xiaozhi` protocol definitions.
* `config/placeholders.py` — `is_placeholder()`; shipped placeholders are `<your-...>`.
* `KIVO_CONFIG`, `KIVO_SERVER_HOST`, `KIVO_SERVER_PORT`, `KIVO_HTTP_PORT`, `KIVO_LOG_LEVEL`
  (`config/config_loader.py`).
* `protocols:` config block (`kivo.enabled`, `legacy_xiaozhi.enabled`, `strict`).
* Tests: `tests/robot/test_protocol.py`, `tests/config/test_kivo_config.py`,
  `tests/core/test_http_routes.py`, `tests/core/test_ws_path_gate.py`, `tests/test_imports.py`;
  `tests/fixtures/test_config.yaml` + `KIVO_CONFIG` in `tests/conftest.py` (two module-level
  skips removed).
* `scripts/smoke_check.py` — exercises a running server on every protocol route.
* This documentation tree (`docs/*.md`), `main/kivo-server/CLAUDE.md`.

## Public endpoints

| Route | Status |
|---|---|
| `ws://host:8000/kivo/v1/` | **new**, default advertised WebSocket URL |
| `http://host:8003/kivo/ota/`, `/kivo/ota/download/{filename}` | **new** |
| `ws://host:8000/xiaozhi/v1/` | retained — `protocols.legacy_xiaozhi.enabled` (default `true`) |
| `http://host:8003/xiaozhi/ota/`, `/xiaozhi/ota/download/{filename}` | retained — same switch |
| `http://host:8003/mcp/vision/explain` | unchanged |
| any other WebSocket path | accepted (inherited behaviour) unless `protocols.strict: true` |

The OTA response advertises `/kivo/v1/` by default; set `server.websocket` to advertise
something else (e.g. behind TLS).

## Configuration migration

| Old | New | Behaviour |
|---|---|---|
| `xiaozhi:` | `hello:` | alias applied to `config.yaml`, the user file and remote config separately; both present in one file → `hello` wins, warning logged |
| placeholder values containing `你`/`你的` | `<your-...>` | both markers recognised by `is_placeholder()`; user files with old placeholders keep working |
| (none) | `protocols:` | absent → both protocols enabled, non-strict |
| `exit_commands: [退出, 关闭]` | `exit, quit, goodbye` added | old phrases kept |
| `wakeup_words` (Chinese phrases) | `hey kivo`, `hi kivo`, `hello kivo` added | Chinese phrases kept: shipped firmware wake-word models emit them |
| `system_error_response` (Chinese) | English | |
| `plugins.get_weather.api_key` (a shared real key) | `<your-qweather-api-key>` | the shared key is no longer committed |
| `manager-api:` block, `read_config_from_api` | unchanged, **deprecated** | remote-config mode against the removed console; no server ships with Kivo |

Environment variables win over both config files.

## Compatibility aliases retained

* `xiaozhi:` config key → `hello:` (with warning)
* `/xiaozhi/v1/`, `/xiaozhi/ota/` routes (`legacy_xiaozhi` protocol)
* `你` placeholder marker
* `manager-api` / `read_config_from_api` code paths (`config/manage_api_client.py`,
  `core/handle/reportHandle.py`, branches in `core/connection.py`, `core/api/vision_handler.py`,
  `core/http_server.py`) — dormant unless `manager-api.url` is set
* `[device_call]` prefix handling in `core/handle/textHandler/listenMessageHandler.py` (device-to-device
  call feature that needed the console; harmless without it)

## Remaining legacy identifiers and why

| Where | Why it stays |
|---|---|
| `robot/protocol/legacy_xiaozhi.py`, `protocols.legacy_xiaozhi`, tests | the compatibility protocol is named after what it is compatible with |
| `config/config_loader.py` `DEPRECATED_KEYS = {"xiaozhi": "hello"}` | the alias must spell the old key |
| `scripts/sync-upstream.sh` `UPSTREAM_URL` | points at the upstream repository |
| `LICENSE` `Copyright (c) 2025 xinnan-tech` | required MIT attribution |
| `config.yaml` wake-word phrases `你好小智` etc. | functional data matched against firmware wake-word output |
| `core/utils/textUtils.py` Chinese punctuation tables, `cnlunar`/`get_lunar` | functional text processing / lunar-calendar feature |
| provider adapters for Chinese vendors (Aliyun, Doubao, Xunfei, Tencent, Baidu, ChatGLM, Coze, Dify…) | working integrations; documented as region-specific in [providers.md](providers.md) |
| `docs/upstream.md`, this file | provenance and migration record |

`Cozmo` appears only as inspiration in the README. Chinese text remains only in the functional
data above and in tests of Chinese text handling (`tests/core/utils/test_textUtils.py`,
lunar-date tests).

## Components removed and why

| Component | Verdict | Reasons |
|---|---|---|
| `manager-api` (Spring Boot, Java package `xiaozhi.*`, MySQL + Redis + Liquibase) | removed | Xiaozhi assistant admin console (agents, prompt templates, voice clones, SMS sign-up, knowledge bases, device address book). Robot management for Kivo is planned inside the Python backend ([robot-roadmap.md](robot-roadmap.md) Phase 7). 411 of 488 files contained Chinese; rebranding meant renaming every Java package. The Python server's dependency on it is optional (`manager-api.url` unset → local config). |
| `manager-web` (Vue 2) | removed | UI for manager-api only |
| `manager-mobile` (uni-app) | removed | Wi-Fi provisioning + agent config against manager-api only |
| `digital-human` (browser client + Chinese wake-word runtime) | removed | a Xiaozhi test client, not backend; Kivo's simulator is planned in Python (`robot/simulator/`) |
| `plugins/preprocess_plugin` | removed | smart-home voice-command matching on Chinese templates (`intents.yaml`) |
| `plugins_func/functions/call_device.py` | removed | called `manager-api` endpoints |

Everything removed is recoverable from git history (`git show ed27727:main/manager-api/...`).

## Database

Kivo Server has no database. The removed console owned the MySQL schema (`ai_*`, `sys_*` tables);
no migration debt remains in this repository.

## Pending admin actions (not done by the migration)

1. Rename the GitHub repository: `gh repo rename Kivo --repo AA-Box/Cozmo` (redirects keep old clones working), then update the clone URL in the README.
2. Enable GitHub Actions for the repository (it is disabled; no workflow has ever run).
3. Publish the first base image: run the **Build Base Image** workflow (`workflow_dispatch`) so `Dockerfile-server` can pull `ghcr.io/aa-box/kivo-server:base`; until then build both images locally with `make docker-build`.
