# Robot getting started

From `git clone` to a simulated robot that talks, moves and decides for itself. Every
command below is one you can paste; nothing is a placeholder for a cloud account, and no
step needs hardware.

Where the other pages take over:
[robot-architecture.md](robot-architecture.md) for why the layers are where they are,
[robot-protocol.md](robot-protocol.md) for what a device puts on the wire,
[behavior-system.md](behavior-system.md) for autonomy,
[safety-model.md](safety-model.md) for what the backend can and cannot promise, and
[deployment.md](deployment.md) for running it somewhere other than your laptop.

## 0. What you need

* Python 3.12
* `libopus` — macOS: `brew install opus`; Debian/Ubuntu: `sudo apt-get install -y libopus0`
* `ffmpeg`, for the audio pipeline: `brew install ffmpeg` / `sudo apt-get install -y ffmpeg`

No GPU, no model download, no API key. The steps below never reach the network after
`pip install`.

## 1. Clone and install

```bash
git clone https://github.com/AA-Box/Nilo.git nilo
cd nilo/main/nilo-server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

That is the **development slice**: lint, types, the unit suite and the robot subsystem.
The end-to-end suite and the server itself also need the runtime slice:

```bash
pip install -r requirements.txt
```

On macOS `vosk` is skipped automatically. The full install is large because it carries
torch for the local speech models; nothing in this page uses them.

## 2. Check out the tree before you run anything

```bash
cd ../..            # back to the repository root
make lint
make typecheck
make test
```

`make test` is green on a clean clone. If it is not, stop here: everything below assumes
the suite passes, and a failure now is a broken environment rather than a broken robot.

## 3. Start the backend

The server needs a configuration file. The shipped `config.yaml` is the defaults; your own
settings go in `data/.config.yaml`, which is git-ignored, or in a file you point
`NILO_CONFIG` at.

```bash
cd main/nilo-server
mkdir -p data
cat > data/.config.yaml <<'YAML'
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8003
log:
  log_level: INFO
YAML
python app.py
```

You should see, among the startup lines:

```
WebSocket endpoint [nilo]:	ws://<your-ip>:8000/nilo/v1/
Vision endpoint:	http://<your-ip>:8003/mcp/vision/explain
```

Leave it running. Everything after this happens in a second terminal.

### The management API and the dashboard

The robot management API runs on its own port with its own credential, and it **fails
closed**: with no token, every route except `/health`, `/ready` and `/api/meta` refuses.

```bash
NILO_ROBOT_ADMIN_TOKEN=dev python app.py
```

Then <http://127.0.0.1:8010/> is the development dashboard, and:

```bash
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8010/ready
curl -s -H 'Authorization: Bearer dev' http://127.0.0.1:8010/metrics | head
```

See [robot-api.md](robot-api.md) for the three gates in front of anything that can move a
robot, and [observability.md](observability.md) for what the metrics mean.

## 4. Connect a simulated robot

In a second terminal, with the same virtualenv:

```bash
cd nilo/main/nilo-server
source .venv/bin/activate
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --robot-id nilo-sim-01
```

The server log should show the session, the MCP handshake and capability discovery. Ask
the API what it now knows:

```bash
curl -s -H 'Authorization: Bearer dev' http://127.0.0.1:8010/api/robots | python -m json.tool
curl -s -H 'Authorization: Bearer dev' http://127.0.0.1:8010/api/robots/nilo-sim-01/capabilities | python -m json.tool
```

Sixteen tools, `"mcp": true`, and telemetry arriving. The simulator also serves its own
view of itself:

```bash
python -m robot.simulator --status
```

## 5. Your first motion command

The management API can move a robot, and it is the same path a model's request takes: a
typed action, the same safety policy, the same executor.

```bash
curl -s -X POST http://127.0.0.1:8010/api/robots/nilo-sim-01/move \
  -H 'Authorization: Bearer dev' -H 'Content-Type: application/json' \
  -d '{"distance_mm": 300, "speed_mmps": 150}' | python -m json.tool
```

Watch the simulator's pose change, and ask for the action's record:

```bash
curl -s -H 'Authorization: Bearer dev' \
  http://127.0.0.1:8010/api/robots/nilo-sim-01/actions | python -m json.tool
```

Now try one the policy refuses, and read the reason rather than a bare `false`:

```bash
curl -s -X POST http://127.0.0.1:8010/api/robots/nilo-sim-01/move \
  -H 'Authorization: Bearer dev' -H 'Content-Type: application/json' \
  -d '{"distance_mm": 99000}' | python -m json.tool
```

`distance_limit_exceeded`. Nothing reached the device: safety is evaluated before dispatch
and again immediately before it (see [robot-actions.md](robot-actions.md)).

## 6. Your first conversation

Conversation needs a language model, and this page promised no API key. Two honest
options:

**A local model through the inherited provider layer.** Any OpenAI-compatible endpoint
works — Ollama, LM Studio, vLLM. In `data/.config.yaml`:

```yaml
selected_module:
  LLM: LocalLLM
LLM:
  LocalLLM:
    type: openai
    base_url: http://127.0.0.1:11434/v1
    model_name: qwen2.5:7b
    api_key: not-needed
```

Restart the server, then speak to the robot from the simulator's own microphone:

```bash
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --scenario conversation
```

**No model at all.** The robot still connects, still reports, still obeys safety and still
runs its deterministic behaviours — it just has less to say. That is not a degraded mode
somebody bolted on; it is the property the whole split exists to have, and there is a test
for it (`tests/e2e/test_scenarios.py`, scenario I).

To see a conversation with no model and no network at all, run the end-to-end suite, which
uses a rule-based local model:

```bash
cd nilo/main/nilo-server
pytest tests/e2e -q
open tmp/e2e-report.md          # every state transition of all ten scenarios
```

## 7. Your first autonomous behaviour

Autonomy runs without anybody asking for it. The fastest way to see the decision is to ask
the behaviour engine what it *would* do in a situation:

```bash
python -m robot.behavior explain --situation person_arrives
python -m robot.behavior explain --situation low_battery
```

Then let it happen for real. Run the simulator with a scenario that puts a person in the
room:

```bash
python -m robot.simulator --server ws://127.0.0.1:8000/nilo/v1/ --scenario person_enters_room
```

and watch the robot decide:

```bash
curl -s -H 'Authorization: Bearer dev' \
  'http://127.0.0.1:8010/api/robots/nilo-sim-01/behavior?fresh=1' | python -m json.tool
```

The reply is the whole decision: what was selected, what else was eligible, what each one
scored and why. A robot that cannot explain itself is a robot nobody can debug
([behavior-system.md](behavior-system.md)).

## Where to go next

| You want to | Read |
| :--- | :--- |
| Know what a device must implement | [robot-protocol.md](robot-protocol.md) |
| Add a behaviour | [behavior-system.md](behavior-system.md) |
| Understand what safety does and does not promise | [safety-model.md](safety-model.md) |
| Give the model a new capability | [robot-agent.md](robot-agent.md) |
| Run it somewhere real | [deployment.md](deployment.md) |
| Know what actually works today | [`PROJECT_STATUS.md`](../PROJECT_STATUS.md) |

## When it does not work

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `Failed to load the Opus library` | no libopus | `brew install opus` / `apt-get install libopus0` |
| Server exits reading the config | no `data/.config.yaml` and no `NILO_CONFIG` | create the file in step 3 |
| Simulator connects, no tools discovered | the device never answered `tools/list` | check the server log for `capability discovery timed out` |
| Every move is `sensor_data_stale` | telemetry stopped arriving | the device is not sending `notifications/telemetry`; see [robot-protocol.md](robot-protocol.md) |
| API returns 401 | no admin token | `NILO_ROBOT_ADMIN_TOKEN=dev` before `python app.py` |
| API returns 403 on a move | the request came from off-box | control endpoints are loopback-only unless `NILO_ROBOT_API_ALLOW_REMOTE_CONTROL` is set |
| `pytest tests/e2e` skips everything | the dev slice only | `pip install -r requirements.txt` |
