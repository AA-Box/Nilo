# Branding and naming

| Thing | Name |
|---|---|
| Product | **Nilo** |
| Backend | **Nilo Server** (`nilo-server`) |
| Repository | `AA-Box/Nilo` (target; the GitHub rename from `AA-Box/Cozmo` is an admin action, see [migration.md](migration.md)) |
| Python namespace for robot code | `robot` (product-neutral, see below) |
| Environment prefix | `NILO_` |
| Container / image prefix | `nilo-` — `ghcr.io/aa-box/nilo-server:{base,latest,X.Y.Z}` |
| Device-facing routes | `/nilo/v1/` (WebSocket), `/nilo/ota/` (HTTP) |
| Future services | `nilo-manager`, `nilo-web`, `nilo-simulator`, `nilo-firmware`, `nilo-protocol` |

One-line description, used wherever the project is introduced:

> Nilo is an open backend platform for autonomous social robots with voice, vision, memory, personality, and embodied behavior.

## Where the product name goes

Product branding belongs at **product boundaries** and nowhere else:

* process and container names (`nilo-server`), image tags, the startup log line
* environment variables (`NILO_CONFIG`, `NILO_SERVER_HOST`, `NILO_SERVER_PORT`, `NILO_HTTP_PORT`, `NILO_LOG_LEVEL`)
* device-facing routes (`/nilo/v1/`, `/nilo/ota/`) and the `nilo` entry of the `protocols:` config block
* documentation, README, templates, the `clientInfo.name` a server sends when it initialises MCP on a device

## Domain code is product-neutral

Inside the code, use the generic domain vocabulary. The name of the product does not
appear in class names, modules or config keys that describe robot concepts:

| Use | Do not use |
|---|---|
| `Robot`, `RobotSession`, `RobotConnection`, `RobotRegistry` | `NiloRobotSession`, `NiloRobotRegistry` |
| `RobotProtocol`, `ProtocolSpec`, `ProtocolRegistry`, `RobotTransport` | `NiloProtocolRegistry` |
| `RobotCapability`, `RobotAction`, `ActionExecutor` | `NiloAction` |
| `BehaviorEngine`, `WorldState`, `MemoryStore`, `VisionPipeline`, `AudioPipeline`, `AgentRuntime` | `NiloWorldState`, `NiloBehaviorEngine` |

The one place a product-ish name is legitimate inside the code is the **name of a protocol**:
`robot/protocol/nilo.py` defines the `nilo` protocol — the only one there is — because that name
is what appears on the wire (`/nilo/v1/`, `/nilo/ota/`) and in the `protocols:` config block.

## Legacy names

* **Xiaozhi** names nothing Nilo has. The compatibility protocol was deleted, `/xiaozhi/v1/`
  and `/xiaozhi/ota/` are no longer served (`ProtocolRegistry.strict` defaults to true, so a
  WebSocket path matching no protocol is rejected with 404), and the deprecated `xiaozhi:` →
  `hello:` config alias is gone — `config_loader.DEPRECATED_KEYS` is empty. No page, comment or
  log line may present a legacy route, a legacy protocol or Xiaozhi device compatibility as a
  current feature: there is no compatibility, fallback or migration path for devices flashed with
  that firmware; they must be reflashed against `/nilo/ota/`. The word survives only as a record
  of where the code came from, or as a guard that the retired routes stay retired:
  * the upstream remote and commit subject in `scripts/sync-upstream.sh` (`LICENSE` is on the
    allow-list as well, though its inherited copyright line names `xinnan-tech`, not the product)
  * the attribution sentence in `README.md`, engineering provenance ([upstream.md](upstream.md))
    and the migration record ([migration.md](migration.md))
  * this page, in the rules about the word itself
  * the regression tests that hold the removal in place — `tests/robot/test_protocol.py`,
    `tests/core/test_ws_path_gate.py` and `tests/core/test_http_routes.py` (the retired routes),
    `tests/config/test_nilo_config.py` (the dropped `xiaozhi:` alias), `tests/test_compose.py`
    (the retired service names) — and the checkers `scripts/smoke_check.py` (probes the retired
    routes expecting 404) and `scripts/check_docs.py` (holds the allow-list itself,
    `LEGACY_ALLOWED`)

  Anywhere else it is a bug. Xiaozhi is never used to describe Nilo itself.
* **Cozmo** appears only as historical inspiration ("a social robot similar in spirit to Cozmo").
  Nilo is not affiliated with Anki, Digital Dream Labs, Cozmo, Xiaozhi or xinnan-tech.
* No new Chinese-language identifiers, comments, log lines or docs. Functional Chinese *data*
  (the `exit_commands` phrases ASR still has to recognise, the punctuation tables in
  `core/utils/textUtils.py`, the lunar calendar in `core/utils/current_time.py`) is fine and is
  documented where it lives. The wake words are English only — `hey nilo`, `hi nilo`,
  `hello nilo` — the Chinese wake phrases went with legacy firmware support.

## Logging

Log lines carry no product prefix; the format is
`<time>[<version>_<module-abbreviations>][<module tag>]-<level>-<message>` (see `config/logger.py`).
The version is `robot.__version__`. Modules log through `logger.bind(tag=TAG)`, and
`create_connection_logger` binds `selected_module`; those two extras are the only ones the
shipped format renders.

## Checking

```bash
python scripts/check_docs.py          # enforces the allow-list above, mechanically
rg -ni "xiaozhi|小智|cozmo" . -g '!.git'
rg -nP "[\x{4e00}-\x{9fff}]" . -g '!.git'
```

Every hit must fall into one of the categories above; [migration.md](migration.md) lists the
ones that remain intentionally.
