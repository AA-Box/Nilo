# Nilo

Nilo is an open backend platform for autonomous social robots with voice, vision, memory,
personality, and embodied behavior.

## What Nilo is

Nilo Server is the brain that a small social robot talks to. Today it is a production-grade
**voice session server**: a robot (or any compatible device) opens a WebSocket, streams Opus audio,
and the server runs voice activity detection, speech recognition, an LLM with tool calling, and
text-to-speech, streaming audio back. Tools reach the device through MCP, so the robot's own
hardware capabilities are exposed to the agent as callable tools.

Around that core, Nilo is growing a **robot domain layer** — world state, behaviour, personality,
an action executor and a safety policy — so the same server can drive an expressive robot rather
than a smart speaker. The direction is described in [docs/robot-architecture.md](docs/robot-architecture.md).

## Current status

**Implemented** (in `main/nilo-server/`, verified against the code):

* WebSocket device sessions with a JSON control channel and binary Opus audio (`core/connection.py`)
* A Nilo-owned device protocol (`robot/protocol/`): WebSocket on `/nilo/v1/`, OTA on `/nilo/ota/`;
  routes come from a registry, not from hard-coded paths, and unknown paths are rejected
* HTTP OTA bootstrap that hands devices their WebSocket URL, auth token and firmware updates from `data/bin/`
* Audio pipeline: Silero VAD, streaming and batch ASR, sentence-chunked streaming TTS, barge-in (`abort`)
* Provider abstraction with 17 ASR, 21 TTS, 17 LLM, 3 vision-LLM, 4 memory and 3 intent configurations ([docs/providers.md](docs/providers.md))
* Unified tool system: server plugins, device IoT descriptors, device MCP tools, remote MCP endpoints, server-side MCP servers ([docs/mcp.md](docs/mcp.md))
* Vision: `/mcp/vision/explain` runs a vision-LLM over a device camera frame
* Configuration layering (`config.yaml` → user file → `NILO_*` environment), HMAC device tokens, structured logging
* Robot domain layer (`main/nilo-server/robot/`): typed models for identity, capabilities, connection,
  telemetry and world state; an async registry of connected robots that survives reconnects; a state-store
  abstraction; a bounded in-process event bus; and capability discovery over the device MCP tool channel
  ([docs/robot-domain.md](docs/robot-domain.md))
* Test suite, lint (Ruff), strict typing for `robot/` (mypy), Docker image and Compose file

* Robot simulator (`main/nilo-server/robot/simulator/`): a complete simulated robot that connects over the real
  WebSocket route, publishes 15 hardware capabilities as device MCP tools, moves over time, senses a configurable
  room, renders camera frames, reports telemetry as MCP notifications, injects failures and plays scenarios
  ([docs/robot-simulator.md](docs/robot-simulator.md))
* Device telemetry ingestion (`main/nilo-server/robot/telemetry.py`): `notifications/*` frames become world state
  and typed events instead of a log line

**In development:**

* Semantic action vocabulary (`move`, `turn`, `look_at`, `follow`, `play_animation`, `stop`) and the action executor

**Planned:** behaviour engine, personality and emotion model, world model, on-device vision
pipeline, robot memory, management API, physical robot firmware. See
[docs/robot-roadmap.md](docs/robot-roadmap.md).

## Architecture

```
robot / device ──WebSocket + Opus──▶ nilo-server
                                      ├─ inherited infrastructure (core/)
                                      │    session handler · VAD → ASR → LLM → TTS · tool system · MCP · OTA · vision
                                      └─ Nilo robotics layers (robot/)
                                           protocol registry ✓ · robot registry ✓ · state ✓ · events ✓ · capabilities ✓
                                           telemetry ✓ · simulator ✓ · actions · safety · behaviour · world map · memory
```

The inherited infrastructure is a stable, well-tested voice pipeline; Nilo adds robotics on top
of it through a few explicit seams instead of rewriting it. Details: [docs/architecture.md](docs/architecture.md)
(what runs today) and [docs/robot-architecture.md](docs/robot-architecture.md) (where it is going).

## Quick start

Requirements: Python 3.12, `ffmpeg` on the PATH, an LLM API key (any OpenAI-compatible endpoint).
On Linux install `libopus0`; macOS and Windows use the bundled Opus libraries in `libs/`.

```bash
git clone https://github.com/AA-Box/Nilo.git nilo
cd nilo/main/nilo-server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Download the local speech-recognition model (FunASR SenseVoiceSmall, ~900 MB) — or pick a cloud ASR in the next step and skip this:

```bash
modelscope download iic/SenseVoiceSmall model.pt --local-dir models/SenseVoiceSmall
```

Create your configuration file. It overrides keys from `config.yaml`; everything else keeps its default:

```bash
mkdir -p data && cat > data/.config.yaml <<'YAML'
selected_module:
  LLM: MyLLM
LLM:
  MyLLM:
    type: openai
    url: https://api.openai.com/v1/
    model_name: gpt-4o-mini
    api_key: sk-your-key
YAML
```

Start the server:

```bash
python app.py
```

The log prints the OTA and WebSocket endpoints. Point a device at `http://<host>:8003/nilo/ota/`
and it receives the WebSocket URL to connect to, or check the routes yourself:

```bash
python ../../scripts/smoke_check.py
```

More: [docs/getting-started.md](docs/getting-started.md).

## Configuration

Three layers, later wins: `main/nilo-server/config.yaml` (shipped defaults, fully commented) →
the user file (`data/.config.yaml`, or the file named by `NILO_CONFIG`) → environment variables
`NILO_SERVER_HOST`, `NILO_SERVER_PORT`, `NILO_HTTP_PORT`, `NILO_LOG_LEVEL`. Providers are chosen in
`selected_module`; each provider block's `type` names the adapter module. Full reference:
[docs/configuration.md](docs/configuration.md).

## Supported AI providers

Derived from `main/nilo-server/core/providers/`:

| Kind | Adapters |
|---|---|
| ASR | FunASR (local), FunASR server, sherpa-onnx (local), Vosk (local), OpenAI-compatible (incl. Groq), Doubao, Doubao streaming, Tencent, Aliyun, Aliyun streaming, Aliyun Bailian streaming, Baidu, Xunfei streaming, Qwen3 ASR Flash |
| TTS | Edge TTS, OpenAI-compatible, Doubao, Huoshan double-stream, Aliyun, Aliyun streaming, Aliyun Bailian streaming, Tencent, Minimax, SiliconFlow CosyVoice, Fish Speech, GPT-SoVITS v2/v3, Index TTS, PaddleSpeech, Xunfei streaming, Coze CN, custom HTTP |
| LLM | OpenAI-compatible (OpenAI, DeepSeek, Doubao, ChatGLM, LM Studio, Volces gateway…), Ollama, Gemini, Dify, Coze, FastGPT, Langflow, Xinference, Aliyun Bailian apps, Home Assistant conversation |
| Vision LLM | OpenAI-compatible (ChatGLM-4V, Qwen-VL, Xunfei Spark) |
| VAD | Silero |
| Memory | none, local short-term summary, mem0ai, PowerMem, report-only |
| Intent | function calling, LLM intent classifier, none |

Module and class names per adapter: [docs/providers.md](docs/providers.md).

## Robot architecture

```
Robot
├── Perception (audio ✓, vision, sensors)
├── World Model
├── Memory
├── Behavior Engine
├── Agent (LLM + tools ✓)
├── Action Executor
├── Safety
└── Device Protocol ✓
```

The rule that shapes everything: **the LLM never controls motors.** It requests semantic actions —
`move`, `turn`, `look_at`, `follow`, `play_animation`, `stop` — with bounded parameters; the
backend validates them against a safety policy; the robot's firmware executes trajectories and
owns acceleration limits, collision and cliff avoidance, watchdogs and emergency stop. See
[docs/robot-architecture.md](docs/robot-architecture.md) and [docs/safety.md](docs/safety.md).

## Development

```bash
cd main/nilo-server
pip install -r requirements-dev.txt     # test/lint tooling + the slice the tests need
ruff check .                            # lint
mypy                                    # strict types for robot/
pytest -q                               # tests (heavy ones skip without the full requirements)
```

Nilo-specific code goes under `main/nilo-server/robot/`; inherited code under `core/` is edited
as little as possible. Layout, conventions and where things go: [docs/development.md](docs/development.md).

## Testing

`pytest -q` in `main/nilo-server` (the robot, simulator and end-to-end suites included);
`make test`, `make lint`, `make typecheck` from the repository root; `scripts/smoke_check.py`
against a running server. CI runs lint + mypy, the suite on Python 3.12 with full and with
dev-only dependencies, and validates the Compose file. Details: [docs/testing.md](docs/testing.md).

## Docker

```bash
make docker-build                       # ghcr.io/aa-box/nilo-server:base and :latest, locally
cd main/nilo-server && docker compose up -d
```

The Compose file mounts `./data` (your config) and the FunASR model file, exposes 8000 (WebSocket)
and 8003 (HTTP) and takes `NILO_*` variables. Deployment options: [docs/deployment.md](docs/deployment.md).

## Roadmap

Protocol registry and migration (done) → robot skeleton, event bus, simulator (done) → device registry
and session adapter (done) → action system and safety policy → semantic action vocabulary → vision and
world model → behaviour engine and personality → management API, memory, multi-robot → hardware
bring-up. Acceptance criteria per phase: [docs/robot-roadmap.md](docs/robot-roadmap.md).

## Documentation

Start at [docs/getting-started.md](docs/getting-started.md). The full tree: architecture,
configuration, development, deployment, protocol, MCP, audio, providers, robot architecture,
robot simulator, safety, testing, upstream provenance, branding, migration — all under
[docs/](docs/).

## Origins and attribution

Nilo began from open-source infrastructure from
[xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) and has
since been adapted toward a general autonomous robotics backend; it no longer shares that
project's device protocol or endpoints. The engineering record of the origin is in
[docs/upstream.md](docs/upstream.md). Nilo is inspired by social robots such as Cozmo but is not
affiliated with Anki, Digital Dream Labs or any upstream project.

Licensed under the MIT License; see [LICENSE](LICENSE), which retains the original copyright notice.
