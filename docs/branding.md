# Branding and naming

| Thing | Name |
|---|---|
| Product | **Kivo** |
| Backend | **Kivo Server** (`kivo-server`) |
| Repository | `AA-Box/Kivo` (target; the GitHub rename from `AA-Box/Cozmo` is an admin action, see [migration.md](migration.md)) |
| Python namespace for robot code | `robot` (product-neutral, see below) |
| Environment prefix | `KIVO_` |
| Container / image prefix | `kivo-` — `ghcr.io/aa-box/kivo-server:{base,latest,X.Y.Z}` |
| Device-facing routes | `/kivo/v1/` (WebSocket), `/kivo/ota/` (HTTP) |
| Future services | `kivo-manager`, `kivo-web`, `kivo-simulator`, `kivo-firmware`, `kivo-protocol` |

One-line description, used wherever the project is introduced:

> Kivo is an open backend platform for autonomous social robots with voice, vision, memory, personality, and embodied behavior.

## Where the product name goes

Product branding belongs at **product boundaries** and nowhere else:

* process and container names (`kivo-server`), image tags, the startup log line
* environment variables (`KIVO_CONFIG`, `KIVO_SERVER_HOST`, `KIVO_SERVER_PORT`, `KIVO_HTTP_PORT`, `KIVO_LOG_LEVEL`)
* device-facing routes (`/kivo/v1/`, `/kivo/ota/`) and the `kivo` entry of the `protocols:` config block
* documentation, README, templates, the `clientInfo.name` a server sends when it initialises MCP on a device

## Domain code is product-neutral

Inside the code, use the generic domain vocabulary. The name of the product does not
appear in class names, modules or config keys that describe robot concepts:

| Use | Do not use |
|---|---|
| `Robot`, `RobotSession`, `RobotConnection`, `RobotRegistry` | `KivoRobotSession`, `KivoRobotRegistry` |
| `RobotProtocol`, `ProtocolSpec`, `ProtocolRegistry`, `RobotTransport` | `KivoProtocolRegistry` |
| `RobotCapability`, `RobotAction`, `ActionExecutor` | `KivoAction` |
| `BehaviorEngine`, `WorldState`, `MemoryStore`, `VisionPipeline`, `AudioPipeline`, `AgentRuntime` | `KivoWorldState`, `KivoBehaviorEngine` |

The one place a product-ish name is legitimate inside the code is the **name of a protocol**:
`robot/protocol/kivo.py` defines the `kivo` protocol and `robot/protocol/legacy_xiaozhi.py`
the compatibility protocol, because those names are what appears on the wire and in config.

## Legacy names

* **Xiaozhi** appears only where it is factually correct: the compatibility protocol
  (`legacy_xiaozhi`, `/xiaozhi/v1/`, `/xiaozhi/ota/`), the deprecated `xiaozhi:` config key alias,
  engineering provenance ([upstream.md](upstream.md)), the license, and the migration record.
  It is never used to describe Kivo itself.
* **Cozmo** appears only as historical inspiration ("a social robot similar in spirit to Cozmo").
  Kivo is not affiliated with Anki, Digital Dream Labs, Cozmo, Xiaozhi or xinnan-tech.
* No new Chinese-language identifiers, comments, log lines or docs. Functional Chinese *data*
  (wake-word phrases emitted by shipped firmware models, punctuation tables, lunar calendar) is fine
  and is documented where it lives.

## Logging

Log lines carry no product prefix; the format is
`<time>[<version>_<module-abbreviations>][<module tag>]-<level>-<message>` (see `config/logger.py`).
The version is `robot.__version__`. Prefer structured `logger.bind(...)` fields — `device_id`,
`session_id`, `client_id`, `protocol` — over embedding identifiers in prose.

## Checking

```bash
rg -ni "xiaozhi|小智|cozmo" . -g '!.git'
rg -nP "[\x{4e00}-\x{9fff}]" . -g '!.git'
```

Every hit must fall into one of the categories above; [migration.md](migration.md) lists the
ones that remain intentionally.
