# Robot backend architecture

How the Cozmo robot subsystem integrates with the vendored Xiaozhi server.

**Status: design only.** `main/xiaozhi-server/robot/` does not exist yet. Everything
below describes what will be built and, crucially, *where it attaches*. The audit in
§1–§3 describes code that exists today and was read line by line; every claim carries a
`file:line`. The design in §4 onward does not exist yet and says so.

Companion documents:

* [`docs/robot-roadmap.md`](robot-roadmap.md) — phases and acceptance criteria.
* [`docs/upstream-strategy.md`](upstream-strategy.md) — how upstream is vendored and merged.
* [`README.md`](../README.md) — the product vision this architecture serves.

---

## 0. The one-paragraph version

The Xiaozhi server is a per-connection voice pipeline: one `ConnectionHandler` per
WebSocket, a shared asyncio loop, a 5-worker thread pool per connection, and a tool layer
that hands OpenAI-shaped function schemas to an LLM. It has no concept of a device that
exists between connections, no session registry, no cancellation, and no preemption. We
do not change any of that. We add `main/xiaozhi-server/robot/` — a modular monolith that
owns the device registry, world state, action system, safety policy, behaviour engine,
personality and animation — and attach it through **four existing registries** plus a
**14-line budget of edits to upstream files**. Semantic actions reach the LLM as ordinary
registered tools; those tool functions do nothing but validate and enqueue. Nothing with a
deadline, and nothing safety-critical, runs on the Xiaozhi event loop or in the Xiaozhi
process at all.

---

## 1. Repository architecture audit

### 1.1 What is in the repository

| Path | What it is | Language | Ours or upstream? |
|---|---|---|---|
| `main/xiaozhi-server/` | The voice-assistant server. 194 Python files, ~30k lines. | Python 3.10 (CI) | upstream + fork edits |
| `main/manager-api/` | Control panel backend ("smart console"): device/agent/model registry, OTA, per-device config. | Java 21 / Spring Boot / Maven | upstream |
| `main/manager-web/` | Control-panel web UI. | Vue / Vite / Vitest | upstream |
| `main/manager-mobile/` | Mobile control panel. | uni-app / TypeScript | upstream |
| `main/digital-human/` | Browser test client that speaks the WebSocket protocol. | web | upstream |
| `.cozmo/` | Fork tooling: `sync-upstream.sh`, 26 archived upstream PR patches. | shell | ours |
| `docs/` | Integration guides + the fork's own strategy docs. | markdown | mixed |

`main/xiaozhi-server/robot/` **does not exist**. Neither does anything robot-specific in
the Python tree. The root `README.md` already describes the robot product in detail; it is
a specification, not a description of code that exists. This document does not contradict
it, but it does flag (§8) which README claims are currently aspirational.

### 1.2 The server, end to end

```
app.py
 ├─ setup_opus()                      app.py:12    hard-fails if libopus is missing
 ├─ load_config()                     app.py:62
 ├─ config["server"]["auth_key"] = …  app.py:67-76 in-memory only, never persisted
 ├─ WebSocketServer(config).start()   app.py:87    port 8000, the device socket
 └─ SimpleHttpServer(config).start()  app.py:89    port 8003, OTA + vision
```

`WebSocketServer.__init__` (`core/websocket_server.py:42-69`) builds VAD, ASR, LLM, Intent
and Memory **synchronously, once, before serving**, and hands the *same objects* to every
connection. `serve()` is called with `ping_interval=None` (`:82`) — there is no
transport-level keepalive — and with **no path routing at all**: any path upgrades, and a
plain HTTP GET on port 8000 answers `"Server is running"` (`:154-161`).

Per connection (`core/connection.py:243`):

```
handle_connection
 ├─ capture running loop, parse headers for device-id           :245-256
 ├─ register_plugins_to_conn(self)                              :267
 ├─ create_task(_check_timeout)                                 :280
 ├─ create_task(_background_initialize)   ← detached, unawaited :293
 └─ async for message in websocket: await _route_message(msg)   :296-297
```

`_background_initialize` fetches per-device config from manager-api and then submits
`_initialize_components` to the per-connection `ThreadPoolExecutor(max_workers=5)`. So
`conn.tts`, `conn.asr`, `conn.func_handler` and the rendered system prompt are `None` for
the first seconds of every connection, and upstream copes with `hasattr` guards and 3-second
poll loops (`core/handle/helloHandle.py:74-80`). **There is no readiness signal.**

Message routing:

```
_route_message(msg)                            connection.py:391
 ├─ gate on bind_completed_event (1s timeout)   :394-401   drops text AND binary
 ├─ if need_bind: drop                          :404-407
 ├─ str   → handleTextMessage                   :412
 └─ bytes → _decode_opus_packet → asr_audio_queue.put()   :424-426
```

Text dispatch is a real, mutable, process-global registry:

```
core/handle/textHandle.py:11        message_registry = TextMessageHandlerRegistry()
core/handle/textMessageHandlerRegistry.py:37   def register_handler(self, handler)
                                        :39       self._handlers[handler.message_type.value] = handler
core/handle/textMessageProcessor.py:33         await handler.handle(conn, msg_json)
                                        :35     unknown type → log at ERROR, drop silently
```

Registered types: `hello, abort, listen, iot, mcp, server, ping`
(`core/handle/textMessageType.py:4-12`).

Audio in: Opus frames are decoded at the socket (`connection.py:575-594`, 16 kHz mono,
960-sample / 1920-byte / 60 ms frames) onto an **unbounded** `queue.Queue`. A daemon thread
per connection pops each frame and marshals it *back onto the event loop* with
`run_coroutine_threadsafe(...).result()` (`core/providers/asr/base.py:43-58`), where
`conn.vad.is_vad()` runs Silero ONNX inference **synchronously on the shared loop**
(`core/handle/receiveAudioHandle.py:21`). Recognized text reaches `startToChat`, which
runs the entire blocking LLM turn on the connection's 5-worker pool
(`receiveAudioHandle.py:127`).

Audio out: `TTSProviderBase` is a two-queue, two-daemon-thread pipeline; Opus frames are
paced onto the socket at 60 ms by `AudioRateController` with `PRE_BUFFER_COUNT = 0`
(`core/handle/sendAudioHandle.py:16,19` — the comment records that ESP32-C3 receive buffers
underrun on burst sends). This pacing is hardware calibration; do not reimplement it.

### 1.3 The tool system

One `UnifiedToolHandler` per connection (`core/providers/tools/unified_tool_handler.py:19`)
owns a `ToolManager` and five executors keyed by a 5-member `ToolType` enum
(`core/providers/tools/base/tool_types.py:10-17`):

| ToolType | Executor | Where the tools come from |
|---|---|---|
| `SERVER_PLUGIN` | `ServerPluginExecutor` | `@register_function` global registry |
| `SERVER_MCP` | `ServerMCPExecutor` | `data/.mcp_server_settings.json`, stdio/sse/http |
| `DEVICE_IOT` | `DeviceIoTExecutor` | deprecated `iot` descriptors |
| `DEVICE_MCP` | `DeviceMCPExecutor` | the device's own `tools/list` |
| `MCP_ENDPOINT` | `MCPEndpointExecutor` | the `mcp_endpoint` WebSocket broker |

`ToolManager.get_function_descriptions()` (`unified_tool_manager.py:49-60`) concatenates
every executor's schemas; `connection.py:1146` passes that list verbatim to
`llm.response_with_functions(...)`. **That list is the entire surface the LLM can act
through.**

Device MCP is JSON-RPC 2.0 tunnelled in `{"type":"mcp","payload":{…}}`
(`core/providers/tools/device_mcp/mcp_handler.py:103-115`). Handshake:
`initialize` id=1 (`:254`) → `sleep(1)` → `tools/list` id=2 (`:277`, continuation `:288`) →
`tools/call` with an id from `MCPClient.get_next_id()`. Tool calls are made by
`call_mcp_tool(conn, mcp_client, name, args, timeout=30)` (`:296`) — a plain module-level
coroutine with explicit arguments, which is what makes it directly reusable.

### 1.4 Configuration

Three layers, and the third one destroys the first:

1. `config.yaml` (defaults) recursively merged with `data/.config.yaml`
   (`config/config_loader.py:150-177`). Unknown top-level keys survive.
2. **If** `data/.config.yaml` sets `manager-api.url`, the entire local config is
   **discarded** and rebuilt from the Java API — only `server.{ip,port,http_port,
   vision_explain,auth_key}`, `manager-api` and `prompt_template` are carried over
   (`config_loader.py:55-84`).
3. Per-device config fetched at connect time and spliced field-by-field into the live
   connection config (`connection.py:875-965`), mutating `self.config` **in place** from a
   background task.

Every module follows `selected_module.<TYPE>` → a config block → the block's `type:` key →
a provider file on disk, resolved with `os.path.exists` on a **CWD-relative path**
(`core/utils/llm.py:17`, `tts.py:35`, `vad.py:13`, `asr.py:18`, `vllm.py:17`,
`memory.py:10`, `intent.py:11`). The server only works when launched from
`main/xiaozhi-server`.

### 1.5 Tests and tooling, before this change

* `pytest` configured in `main/xiaozhi-server/pyproject.toml` (10 lines, `asyncio_mode="auto"`).
* `tests/conftest.py` puts the server root on `sys.path`, so a future top-level `robot/`
  package is importable from tests with no packaging work.
* 45 tests passed, 2 skipped on a clean checkout. They cover `merge_configs`, `textUtils`,
  `Dialogue`/`Message`, `output_counter`, `current_time` — pure functions only.
* **CI ran only `tests/test_smoke.py`** (two asserts); the other 43 never executed.
* **CI triggered on push to `main`**, but the default branch is `develop` — and
  `docs/upstream-strategy.md:39` mandates that upstream merges are resolved *on develop*.
  The single highest-risk operation in the fork was the one operation CI did not cover.
* **No ruff, mypy, pyright, flake8, black or pre-commit config existed anywhere** for
  Python, despite `README.md:757-758,821,827` promising Ruff and mypy/pyright.
* Python version disagreed three ways: CI 3.10, `requirements.txt:2` comment 3.10,
  `Dockerfile-server-base:3` 3.10-slim, `README.md:743` "Python 3.12+".

§7 records what this change does about that.

---

## 2. Xiaozhi protocol: what we may and may not use

Cross-checked against upstream firmware (`78/xiaozhi-esp32@main`: `docs/websocket.md`,
`docs/mcp-protocol.md`, `docs/notify.md`, `main/application.cc`, `main/mcp_server.{h,cc}`,
`main/protocols/*.cc`) and against this fork's implementation. Where the two disagree, the
fork wins for our purposes and the disagreement is noted.

### 2.1 Message types already taken

`abort`, `alert`, `custom`, `goodbye`, `hello`, `iot`, `listen`, `llm`, `mcp`, `notify`,
`ping`, `pong`, `server`, `stt`, `system`, `tts`.

`robot` is free. Both stock sides degrade gracefully on an unknown type — the device logs
`Unknown message type` (`application.cc:716`) and the fork logs at ERROR and drops
(`textMessageProcessor.py:35`). No disconnect, no state corruption. **Compatibility with
normal Xiaozhi clients is therefore preserved by construction**: a stock ESP32 never sends
`type:"robot"`, and never receives one unless it is a robot.

### 2.2 The device MCP type system is the binding constraint

`main/mcp_server.h:61` — **three** argument types: `boolean`, `integer`, `string`. No
float, no enum, no array, no nested object, no per-property description. Consequences we
must design around:

* Every physical quantity is an **integer with the unit in the parameter name**:
  `speed_mmps`, `angle_deg`, `distance_mm`, `duration_ms`, `height_pct`.
* Allowed string values are enumerated **in prose inside the tool description** — that is
  how `self.screen.set_theme` documents `light`/`dark`.
* `Property(name, type, default)` sets a *default*, not a description. Upstream already
  tripped on this: `self.upgrade_firmware`'s `url` has
  `"default": "The URL of the firmware binary file to download and install"`
  (`mcp_server.cc:148`).
* A property is required iff it has no default; `required` is omitted entirely when empty.

Other hard limits:

| Limit | Value | Source |
|---|---|---|
| `tools/list` page size | 8000 bytes | `mcp_server.cc:483` |
| `nextCursor` | a **tool name**, not an opaque token — ordering must be stable | `mcp_server.cc` |
| tool-call timeout (server side) | 30 s default, never overridden | `mcp_handler.py:301`, `mcp_executor.py:41` |
| device channel timeout | 120 s | `protocol.cc:109` |
| inbound WS frame | 1 MiB (`websockets` default; `max_size` never set) | `core/websocket_server.py:76-82` |
| vision upload | 5 MB | `core/api/vision_handler.py:17` |
| tool-name mangling | `.` and every non-`[A-Za-z0-9_-]`/non-CJK char → `_` | `core/utils/util.py:570-573` |

Budget ~300–500 bytes per tool description and the robot's tool list stays on one page.

### 2.3 Existing device tool vocabulary, and what it teaches

The full built-in set is `self.get_device_status`, `self.audio_speaker.set_volume`,
`self.screen.set_brightness`, `self.screen.set_theme`, `self.camera.take_photo`, plus
user-only tools behind `params.withUserTools = true` — which **this fork never sends**
(`mcp_handler.py:273-281`), so `self.reboot`, `self.screen.snapshot` and friends are
currently unreachable.

Three board families show three patterns, and only two of them are worth copying:

* **`self.chassis.*`** (esp-sparkbot, `esp_sparkbot_board.cc:209-245`) —
  `go_forward` / `go_back` / `turn_left` / `turn_right`, **argument-free**, fire-and-forget
  over UART. No distance, no speed, no duration. *Do not copy this.* It is exactly the
  vocabulary a Cozmo backend has to replace.
* **`self.dog.basic_control`** (esp-hi, `esp_hi.cc:333-386`) — one tool, a string `action`,
  allowed values listed in the description. Compact.
* **`self.otto.action`** (`otto_controller.cc:533-550`) and **`self.electron.head_move`**
  (`electron_bot_controller.cc:469-495`) — a fat dispatcher with a string `action` plus
  shared, fully-defaulted integer parameters (`steps`, `speed`, `direction`, `amount`).
  **This is the pattern to copy.** It keeps the tool list small, preserves the prompt-cache
  prefix `AddCommonTools` deliberately maintains (`mcp_server.cc:28-30`), and can express
  magnitude.

Two naming rules follow directly:

* `self.battery.get_level` returning `{"level":83,"charging":false}` is already a
  cross-board convention (otto and electron-bot register it byte-identically). **Reuse the
  name; extend the returned blob.** Do not invent `self.power.*`.
* `self.screen.*` already means the LVGL UI panel. Cozmo's animated face is an actuator and
  must be `self.face.*`.

### 2.4 Vision

Server mints a 1-hour JWT and injects it into the MCP `initialize` as
`capabilities.vision = {url, token}` (`mcp_handler.py:238-270`). Firmware's
`ParseCapabilities` reads **only** `capabilities.vision.*` and silently ignores every
sibling key — which is how vision was retrofitted, and which makes
`capabilities.robot = {…}` a free, wire-safe extension slot. The device POSTs multipart
(`question` then `file`, read **positionally** at `vision_handler.py:72-84`) to
`/mcp/vision/explain`; the handler returns
`{"success":true,"action":"RESPONSE","response":"…"}`, and `mcp_executor.py:50-59` detects
the `action` key and short-circuits — the answer is spoken with **no second LLM pass**.

That `{"action": …}` short-circuit is the cheapest low-latency perception/reaction
mechanism in the codebase. Robot perception tools copy it verbatim rather than inventing
one.

Two operational teeth: the vision token is minted once per connection and never refreshed,
so `self.camera.take_photo` starts returning 401 on sessions older than an hour; and
`Client-Id: web_test_client` bypasses authentication entirely (`vision_handler.py:36-39`).

### 2.5 Server→device push that already works

`notify` (`docs/notify.md`, `application.cc:585-617`) pushes one-way audio plus subtitles
to an **idle** device without opening the mic — implemented in stock firmware and *not*
implemented in this fork. And any MCP method beginning `notifications/` is silently
swallowed by stock firmware (`mcp_server.cc:375-377`), which makes
`{"type":"mcp","payload":{"method":"notifications/robot/…"}}` a no-op on a stock device and
a real signal on robot firmware. Both are free wins; neither breaks anything.

---

## 3. Risky coupling points

Ranked by how much they constrain the robot design. Each was verified against source.

### R1 — There is no session registry (`core/websocket_server.py:121-135`)

`ConnectionHandler` is a local variable; the reference is dropped when
`handle_connection` returns. Nothing maps `device_id` → a live handler. A management API,
behaviour engine or supervisor has no way to reach a connected robot, enumerate robots, or
answer "is device X online". **The robot subsystem's first primitive is its own registry.**

### R2 — Two idle killers close the socket on voice silence

`_check_timeout` (`connection.py:1763-1792`) hard-closes after
`close_connection_no_voice_time + 60` (default 180 s); `no_voice_close_connect`
(`receiveAudioHandle.py:130-154`) speaks a goodbye and closes after 120 s. Both key on
`conn.last_activity_time`, refreshed in five places — including every outbound TTS packet
(`sendAudioHandle.py:227,251`), a `listen` message (`listenMessageHandler.py:57-59`), and
incoming voice. The `ping` refresh (`pingMessageHandler.py:34`) is **unreachable under
stock config** (`enable_websocket_ping` defaults false, `config.yaml:75`).

An always-on robot that is patrolling or idle gets a goodbye and a dropped socket within
2–3 minutes, mid-motion. A robot that streams only telemetry never reaches
`no_voice_close_connect` at all (it is only called from `handleAudioMessage`) and instead
gets a **silent** close at ~180 s.

### R3 — The read loop awaits every handler inline (`connection.py:296-297`)

`async for message in self.websocket: await self._route_message(message)`, and
`process_message` does `await handler.handle(conn, msg_json)`
(`textMessageProcessor.py:33`). A robot handler that awaits real work stalls ingestion of
**all** subsequent frames from that device, audio included, and is indistinguishable from a
dead device. Upstream's own `iot` and `mcp` handlers offload with `create_task`
(`iotMessageHandler.py:20`, `mcpMessageHandler.py:20`); `listen` does not, and blocks up to
3 s (`listenMessageHandler.py:81-84`).

### R4 — The bind gate drops messages, text and binary (`connection.py:391-407`)

Every inbound frame waits up to 1 s on `bind_completed_event` and is discarded on timeout;
once `need_bind` is true, everything is dropped unconditionally. In local-config mode the
event is set almost immediately (`:845-847`), so little is lost — but in manager-api mode a
slow config fetch silently eats the first second, and an unbound device is permanently
mute. **No safety-critical command may traverse this path.**

### R5 — Provider instances are process-global and mutated per connection

`modules_initialize.py:36-136` memoizes constructed **instances** in a process-global cache
keyed by module name; `websocket_server.py:61` builds one memory/LLM/intent object and
hands the *same object* to every `ConnectionHandler` (`:121-126` → `connection.py:177`).
Each connection then mutates it: `_initialize_memory` sets `role_id = self.device_id`
(`connection.py:1006-1011`), `set_llm()` at `:1038`.

Worse for TTS. `TTSProviderBase` holds `self.conn`, `self.current_sentence_id`, its two
queues and its two daemon threads as instance state (`base.py:349-367`). Two devices
sharing a TTS config share one pipeline: the second connection rebinds `self.conn` and
starts a *second* thread pair against the same queues; `conn.close()` then runs
`await self.tts.close()` on the **shared** instance, clearing `_sentence_text_map` and
closing the provider's upstream socket for everybody (`base.py:523-526`), and
`clear_queues()` (`connection.py:1705-1724`) flushes another robot's pending audio. The
threads watch `self.conn.stop_event` re-read each iteration (`base.py:413,459`), so it is
the **last** connection's stop event that kills them all, not the first.

Scope: this sharing path is taken when `read_config_from_api` is true — i.e. the
manager-api multi-device deployment, which is exactly the robot fleet case.

### R6 — Everything safety-critical is missing, and the existing "stop" is cooperative

`client_abort` is a plain bool polled inside the LLM streaming loop
(`abortHandle.py:9-19`, read at `connection.py:1200`). There is **no cancellation path for
an in-flight device tool call**: `call_mcp_tool` registers a future and removes it only on
its own timeout (`mcp_handler.py:399`); `UnifiedToolHandler.cleanup()` never touches
`conn.mcp_client.call_results` (`:228-242`); nothing rejects pending futures when the socket
drops; and there is no message that tells the device to abort what it is doing. If a
`move()` is pending when the connection dies, the server-side future hangs for 30 s while
the robot keeps moving.

And the process can vanish at any instant: a device-sent
`{"type":"server","action":"restart"}`, gated only by a plaintext secret compared with a
non-constant-time `!=` (`serverMessageHandler.py:23-26`), spawns `python app.py` and calls
`os._exit(0)` from a daemon thread (`connection.py:624-631`), skipping every `finally`.

### R7 — The shared event loop is not real-time, by construction

Silero ONNX inference runs on it (`receiveAudioHandle.py:21`); most ASR providers block it
with `requests.post` / synchronous WAV writes (`asr/openai.py:28`, `base.py:276-296`);
`gc_manager` runs `gc.collect()` plus two full `gc.get_objects()` walks every 300 s
(`gc_manager.py:89-96`), holding the GIL for an unbounded pause; and the TTS play thread
does an **untimed** `future.result()` against that loop (`tts/base.py:502-506`), so a loop
stall wedges audio output entirely.

### R8 — The flat tool namespace lets device tools shadow server tools

`ToolManager.get_all_tools()` merges five executors into one flat dict; a collision only
logs a warning and the later executor wins (`unified_tool_manager.py:40-41`). Iteration
order is `SERVER_PLUGIN, SERVER_MCP, DEVICE_IOT, DEVICE_MCP, MCP_ENDPOINT`
(`unified_tool_handler.py:38-52`) — so **device-MCP tools always beat server plugins**. A
server-side guarded `move` would be silently shadowed by a raw device tool also named
`move`, inverting the entire design rule.

### R9 — `intent_llm` freezes its tool list process-wide

The intent provider is a process-wide singleton (`modules_initialize.py:69-83`) and
`intent_llm` caches its rendered prompt on it with `if self.promot == "":`
(`intent_llm.py:169`). The tool list baked into that prompt is whatever the **first**
connection had, frozen for the process lifetime. Robot A connects with a wheeled base;
robot B (no wheels) is then offered `move()`/`turn()` and will emit them. It also lists
every device-MCP tool twice (`:170-176`).

### R10 — JSON-RPC id collision on the device MCP channel

`MCPClient.next_id = 1` (`device_mcp/mcp_client.py:20`) while `initialize` is id 1 and
`tools/list` is id 2 (`mcp_handler.py:254,277,288`). Pending call futures are checked first
(`:134`), which usually saves it — but a paginated `tools/list` continuation arriving while
a tool call holds id 2 resolves that call's future with `{"tools": […]}` and the
continuation page is lost. A robot with enough tools to paginate can silently lose half its
tool list, and a motion command can "succeed" with garbage.

Trap: `MCPClient` is defined **twice, byte-identically** — `mcp_handler.py:19-100` is dead
code and `device_mcp/__init__.py:3` exports the `mcp_client.py` version. Patching the wrong
copy is a no-op that looks correct.

### R11 — Unauthenticated token oracle and an auth bypass

The OTA POST endpoint performs **no authentication** and will mint a valid 30-day
WebSocket auth token for any attacker-chosen device-id/client-id pair
(`ota_handler.py:143-298`, issuance at `:288,:290`); the firmware download route is
likewise unauthenticated. So when `server.auth.enabled` is true, one unauthenticated HTTP
POST defeats the WebSocket auth gate. Separately, `vision_handler.py:36-39` returns
`True` unconditionally for `Client-Id: web_test_client`, trusting a client-supplied
`Device-Id`.

**"Authenticated WebSocket connection" must never by itself authorize actuation.**

### R12 — Smaller things that still shape the design

* `Dialogue` is an unbounded list with no lock, mutated from the executor thread inside
  `chat()` and concurrently from event-loop handlers (`dialogue.py:25-33`); nothing trims it
  by length or tokens, and few-shot examples are re-sent every turn (`:150-155`). A robot
  connected for hours grows the message array until the provider errors.
* `ConnectionHandler` has no fixed attribute set; 13+ attributes are grafted on from other
  modules at arbitrary times (`silero.py:42-44`, `sendAudioHandle.py:99-101`,
  `connection.py:598-599`). Robot state goes under **one** namespaced attribute.
* `setup_logging()` runs at module scope in 81 files and calls `check_config_file()`,
  which raises `FileNotFoundError` when the gitignored `data/.config.yaml` is absent, then
  falls back to `asyncio.run(load_config())` — which raises inside a running loop — and
  finally calls `logger.remove()`, destroying every loguru sink in the process
  (`config/logger.py:49-115`, `config/settings.py:18-24`). This is why two test modules
  skip at module level. **`robot/` must be importable without `data/.config.yaml`.**
* `scan_plugins()` swallows every import exception behind a `print`-based logger
  (`plugins/__init__.py:100-108`), and `register_plugins_to_conn` instantiates *any*
  `BasePlugin` subclass visible in any `plugins.*` module namespace — including re-exported
  ones — with no dedup (`:181-196`, `manager.py:43-66`).
* `load_config()`'s cached `main_config` is never invalidated after a manager-api hot
  reload (`websocket_server.py:163-211` vs `config_loader.py:31,51`), so two divergent
  configs coexist in the process. Read config from the object you were handed.
* `update_config()` rebinds `WebSocketServer.config` only. Already-established connections
  keep the module objects and the deep-copied config they were constructed with
  (`connection.py:128,174-178`); `SimpleHttpServer`/`OTAHandler`/`VisionHandler` captured the
  original dict and never see the new one; `self.auth` is never rebuilt.
* `close()` is reachable from **six** call sites (`connection.py:315,374,1781`,
  `receiveAudioHandle.py:149`, `sendAudioHandle.py:56`, `intentHandler.py:61`) with no
  idempotency flag. Double-teardown is the normal flow, survivable only because the whole
  body sits under a blanket `try/except` (`:1588,1698-1699`).

---

## 4. Proposed integration architecture

### 4.1 Shape

A **modular monolith** in `main/xiaozhi-server/robot/`, in the same process as the Xiaozhi
server, plus a **separate deterministic motion layer** that is *not* in this process and
usually not on this machine.

```
main/xiaozhi-server/
├── app.py                  upstream  (+3 lines: start/stop the robot control plane)
├── core/                   upstream  (+2 lines in connection.py; 3 defect fixes — §6)
├── plugins/
│   └── cozmo_bridge/       OURS, new directory — the adapter, ~150 lines total
│       ├── __init__.py         import-time: boot robot runtime, register handlers
│       └── tools.py            @register_function semantic action vocabulary
└── robot/                  OURS, new tree — no upstream file has this path
    ├── actions/            action model, lifecycle, queue, resource claims
    ├── behavior/           utility-scoring behaviour engine (runs without an LLM)
    ├── devices/            device registry, capability model, session adapter
    ├── events/             in-process async event bus
    ├── memory/             robot-specific memory tiers
    ├── personality/        traits + emotion state
    ├── protocol/           Pydantic models for every robot wire message
    ├── safety/             policy evaluation, limit clamping, watchdog supervision
    ├── simulator/          a fake robot device that speaks the same protocol
    ├── state/              world model, per-robot state
    ├── vision/             perception pipeline, target tracking
    └── api/                REST + WebSocket management API (own aiohttp app, own port)
```

`robot/` never imports from `core/` at module scope except through a thin, lazily-imported
adapter in `robot/devices/`. That keeps `robot/` importable in tests without
`data/.config.yaml` (R12) and keeps the merge surface at zero.

### 4.2 The four seams

```
                          ┌──────────────────────────────────────────┐
   ESP32 / robot          │          Xiaozhi server (upstream)        │
   firmware               │                                          │
      │                   │  websockets.serve  ──► ConnectionHandler  │
      │  ws :8000         │                             │            │
      ├───────────────────┼─────────────────────────────┤            │
      │                   │        ┌────────────────────┴──────┐     │
      │                   │        │ ① message_registry        │     │
      │  {"type":"robot"} │        │   register_handler(...)   │     │
      │ ─────────────────►┼────────►   textHandle.py:11        │     │
      │                   │        └────────────┬──────────────┘     │
      │                   │                     │                    │
      │  {"type":"mcp"}   │        ┌────────────┴──────────────┐     │
      │ ◄────────────────►┼────────┤ ② call_mcp_tool(...)      │     │
      │   tools/call      │        │   mcp_handler.py:296      │     │
      │                   │        └────────────┬──────────────┘     │
      │                   │                     │                    │
      │                   │        ┌────────────┴──────────────┐     │
      │                   │   LLM ─┤ ③ @register_function      │     │
      │                   │        │   IOT_CTL semantic tools  │     │
      │                   │        └────────────┬──────────────┘     │
      │                   │                     │                    │
      │                   │        ┌────────────┴──────────────┐     │
      │                   │        │ ④ scan_plugins()          │     │
      │                   │        │   connection.py:87        │     │
      │                   │        └────────────┬──────────────┘     │
      └───────────────────┴─────────────────────┼────────────────────┘
                                                │
                          ┌─────────────────────▼─────────────────────┐
                          │        robot/  (modular monolith)         │
                          └─────────────────────┬─────────────────────┘
                                                │  semantic requests only
   ════════════════════════════════════════════ │ ═══ process / network boundary ═══
                                                ▼
                          ┌───────────────────────────────────────────┐
                          │   deterministic motion layer (firmware)   │
                          │  trajectories · accel limits · collision  │
                          │  cliff · watchdog · e-stop · motor PWM    │
                          └───────────────────────────────────────────┘
```

**① Inbound robot messages — `{"type":"robot"}`.** `message_registry.register_handler()`
keys purely on `handler.message_type.value` (`textMessageHandlerRegistry.py:39`) with no
`isinstance` check, so a **robot-owned** enum works and
`core/handle/textMessageType.py` needs no edit. Register exactly **one** type with an
internal `op` field — one key is one collision surface with future upstream types. The
handler body must `asyncio.create_task` immediately and return (R3), and must refresh
`conn.last_activity_time` (R2). Telemetry only: R4 means this channel may not carry
safety-critical traffic.

**② Outbound device actuation — `call_mcp_tool`.** A plain module-level coroutine taking
`conn` and the client explicitly (`mcp_handler.py:296`). It already owns JSON-RPC framing,
the envelope, name de-sanitization, result unwrapping and future cleanup. Gate on
`await conn.mcp_client.is_ready()` and `has_tool()`, and **always pass an explicit short
timeout** — the 30 s default is never overridden and is two orders of magnitude too long
for a motion acknowledgement.

**③ The LLM's only action surface — `@register_function(..., ToolType.IOT_CTL)`.**
Registration alone is *not* enough: `ServerPluginExecutor.get_tools()` exposes a function
to the LLM only if its name is in the hardcoded `necessary_functions`, or in
`config["Intent"][selected]["functions"]`, **or** its `type.code == 5` (`IOT_CTL`)
(`plugin_executor.py:99,102-118`). `IOT_CTL` is the only branch that needs **no config
edit**, and it is also the branch that passes `conn` to the handler (`:33-34`) and awaits
`async def` handlers (`:46-48`) — both of which we need. `plugins/preprocess_plugin` already
uses exactly this (`:1139-1157`).

Two traps: import `ToolType` from `plugins.register`, **not** from
`core.providers.tools.base` — same name, different enum. And namespace every tool `robot_*`
(R8).

**④ Bootstrap — `plugins/cozmo_bridge/__init__.py`.** `scan_plugins()`
(`core/connection.py:87` → `plugins/__init__.py:111-148`) importlib-imports every
`plugins/<dir>/__init__.py` at import of `core.connection`. A new directory is never a
merge conflict. Two caveats, both load-bearing: it runs at **import** time, before
`asyncio.run()`, so async work must be deferred; and `_import_module_safe` swallows every
exception behind a `print` (`:100-108`), so the bridge **must assert its own registration**
or a typo silently disables the whole robot subsystem while the server looks healthy.

### 4.3 The upstream edit budget

The zero-edit routes for session lifecycle were evaluated and rejected. Wrapping the
`hello` handler works mechanically, but hello is dropped entirely for unbound devices and
when bind state is unresolved after 1 s (R4), so the hook silently never fires for exactly
the sessions that most need supervision. Deregistration is worse: `conn.stop_event` is a
`threading.Event`, not awaitable, and a `WeakValueDictionary` will not drop entries at
disconnect because `ConnectionHandler` sits in 10+ reference cycles
(`unified_tool_handler.py:23`, `tts/base.py:349`), so entries survive until a generational
GC pass.

So we pay for a small, explicit, documented budget. **Total: 14 lines across 5 upstream
files.** Every line is `try/except`-wrapped so a robot failure can never break a Xiaozhi
session.

| File | Lines | Why |
|---|---|---|
| `core/connection.py` after `:264`, and in the `finally` at `:308` | +2 | Session attach/detach. Teardown at `:308` is guaranteed and deterministic; nothing else is. Fixes R1. |
| `app.py` near `:86-90` | +3 | Start the robot control plane on its own ports with a real asyncio lifecycle and graceful shutdown, instead of hanging it off import-time side effects. Must attach a done-callback that escalates into the safety layer — `ws_task`/`ota_task` are created and never error-checked, so a port conflict today kills a server silently. |
| `core/providers/tools/device_mcp/mcp_client.py:20` | ~1 | `next_id = 100`. Fixes R10. Note: **not** `mcp_handler.py:27` — that class is dead code. |
| `core/providers/tools/device_mcp/mcp_handler.py:221-224` | ~4 | Dispatch inbound `notifications/*` to a robot hook. Purely additive — stock firmware never sends notifications. This is the only way cliff/touch/pickup events can reach the backend as events rather than polls. |
| `core/api/vision_handler.py:36-39` | −4 | Delete the `web_test_client` auth bypass. Fixes half of R11. |

Everything else — tools, message types, providers, prompt, world state, speech — is
zero-edit.

### 4.4 Component responsibilities

| Component | Owns | Explicitly does not own |
|---|---|---|
| `robot/devices/` | Device registry keyed by `device_id`; capability model discovered from `tools/list`; the `ConnectionHandler` adapter. | The WebSocket, auth, opus, VAD/ASR/TTS. |
| `robot/events/` | In-process async event bus; bounded queues with drop-oldest. | Any cross-process transport. |
| `robot/state/` | World model, per-robot pose/battery/sensor state, freshness stamps. | Ground truth — the robot is authoritative; the model is a stale cache with an age. |
| `robot/actions/` | Action model, lifecycle (`PENDING → STARTING → RUNNING → SUCCEEDED/FAILED/CANCELLED/TIMED_OUT/REJECTED`), queue, resource claims over `DRIVE/HEAD/LIFT/DISPLAY/AUDIO/CAMERA`. | Trajectory generation, motor commands. |
| `robot/safety/` | Policy evaluation, clamping of every semantic request against limits and sensor state, watchdog supervision, escalation. | Being *the* safety layer — see below. |
| `robot/behavior/` | Utility-scoring behaviour engine; runs with no LLM available. | Blocking on the LLM. |
| `robot/personality/` | Traits, emotion state as control signals. | Overriding safety. Ever. |
| `robot/vision/` | Perception pipeline, normalized 0.0–1.0 coordinates, target tracking. | The `/mcp/vision/explain` endpoint (R11). |
| `robot/protocol/` | Pydantic models for every robot message; a version field and explicit acks. | — |
| `robot/api/` | REST + WS management API on its own aiohttp app and port. | Routes on `core/http_server.py` (no registry exists there — `:43-76`). |
| `robot/simulator/` | A fake device speaking the same protocol, so CI needs no hardware and no cloud. | — |

**Two safety layers, and the backend is the weaker one.** `robot/safety/` clamps and
rejects; it is a policy filter, not a guarantee. The guarantee lives in firmware, because
this process can be terminated by `os._exit(0)` mid-motion (R6), can be stalled for
hundreds of milliseconds by a GC pass (R7), and has no cancellation path for an in-flight
device call (R6). **Cliff avoidance, collision avoidance, acceleration limits, the
dead-man watchdog and e-stop are duplicated in firmware and fail safe on loss of
heartbeat — not on a Python cleanup callback.**

### 4.5 How a semantic action actually flows

```
user: "come here"
  └─ ASR ─► chat() ─► response_with_functions(functions=[… robot_move …])
       └─ LLM emits robot_move(distance_m=0.3)
            └─ ServerPluginExecutor.execute(conn, "robot_move", {...})   IOT_CTL → func(conn, **args)
                 └─ robot/actions: validate schema, clamp via robot/safety, enqueue
                      └─ return ActionResponse(Action.NONE)         ← returns in microseconds
                           ⋮ (asynchronously)
                      robot/actions dispatcher
                           └─ call_mcp_tool(conn, conn.mcp_client, "self_robot_drive",
                                            {"distance_mm": 300, "speed_mmps": 120},
                                            timeout=2)
                                └─ device: deterministic trajectory, limits, cliff, watchdog
                                     └─ notifications/robot/action_done ─► event bus ─► world state
```

The tool function **never blocks on hardware**. `chat()` waits on each tool future with a
30 s timeout on the shared 5-worker pool (`connection.py:1394-1396`), sequentially per
call, with no cancellation — a tool that waits for motion to finish starves the pool and
can pin a worker indefinitely. Return `Action.NONE` for silent execution, `Action.RECORD`
to log the call into history without a second LLM round-trip
(`connection.py:1468-1510`), or `Action.REQLLM` only when the LLM must narrate the outcome.

**The "no raw PWM" rule is enforced by three things, not one.** The LLM only ever sees
`func_handler.get_functions()` — but that list merges five executors, and device-MCP tools
silently win name collisions (R8). So: (a) every registered robot tool schema exposes only
semantic parameters; (b) every robot tool is namespaced `robot_*`; (c) the bridge asserts
at session start that no device-advertised tool collides with a `robot_*` name, and that
raw-actuator device tools are not exposed to the LLM. A lint rule (§7) mechanically
prevents LLM-facing modules from importing motion primitives.

### 4.6 Deliberate non-choices

* **No new `selected_module.Robot` module type.** It would require coordinated edits in
  `config.yaml`, a new `core/utils/<type>.py`, a new `core/providers/<type>/base.py`,
  `modules_initialize.py:12-21` and `:87-105`, **both** positional `initialize_modules`
  call sites in `websocket_server.py` (`:47-55`, `:184-194`) — where a new mid-signature
  parameter silently shifts every flag — and three sites in `connection.py`. Six permanent
  merge conflicts.
* **No microservices yet.** One process, module boundaries enforced by imports and lint.
  The only split that matters now is backend ↔ motion, and that split is forced by physics,
  not by architecture taste.
* **No robot config in `config.yaml` or `data/.config.yaml`.** A `robot:` key survives
  local mode and is **silently discarded** in manager-api mode (`config_loader.py:66-83`) —
  taking speed, acceleration and geofence limits with it. Robot config lives in its own
  file, loaded by robot code, with its own schema validation.
* **No robot traffic on port 8000 for control.** It inherits the bind gate (R4), no
  keepalive (`ping_interval=None`), per-connection model init, and a console `restart` that
  does `os._exit(0)` mid-motion (R6). Telemetry over `{"type":"robot"}` is fine; commands
  are not.
* **No reuse of `device_iot`.** Tool identity is recovered by positional string-splitting
  on `_` (`iot_executor.py:28-59`), so `head_tilt` mis-routes, and its schema makes the
  **LLM** author response templates (`:154-165`) — the opposite of the design rule.
* **`intent_type: function_call`, never `intent_llm`.** R9.

---

## 5. Dependency diagram

Arrows point from dependent to dependency. `robot/` depends on `core/` at exactly one
place — the adapter — and never the other way around.

```
                                ┌───────────────────────┐
                                │  plugins/cozmo_bridge │   ← the ONLY module that
                                │  (adapter, ~150 loc)  │     imports both sides
                                └───┬───────────────┬───┘
             registers into ───────┘               └─────── imports
                    │                                            │
   ┌────────────────▼────────────────┐          ┌────────────────▼────────────────┐
   │        core/ (upstream)         │          │        robot/ (ours)            │
   │                                 │          │                                 │
   │  handle/textHandle              │          │  api ──────┐                    │
   │  handle/receiveAudioHandle      │          │  behavior ─┼──► actions ──┐     │
   │  providers/tools/*              │          │  personality              │     │
   │  providers/{llm,tts,asr,vad}    │          │      │                    ▼     │
   │  connection.ConnectionHandler   │          │      ▼                 safety   │
   │  websocket_server               │          │   state ◄── vision        │     │
   └─────────────────────────────────┘          │      ▲        │           │     │
                    ▲                           │      │        │           ▼     │
                    │                           │    events ◄───┴────── devices   │
                    │ lazy import, adapter only │      ▲                    │     │
                    └───────────────────────────┼──────┘                    │     │
                                                │  memory ──► state         │     │
                                                │  protocol (leaf)          │     │
                                                │  simulator ──► protocol   │     │
                                                └───────────────────────────┼─────┘
                                                                            │
                                                       call_mcp_tool / robot ws
                                                                            ▼
                                                        ┌───────────────────────────┐
                                                        │ deterministic motion layer│
                                                        │      (firmware / MCU)     │
                                                        └───────────────────────────┘
```

Layering rules, enforced by lint (§7):

| Layer | May import | May **not** import |
|---|---|---|
| `robot/protocol` | stdlib, pydantic | anything else in `robot/` |
| `robot/events` | `protocol` | `actions`, `behavior`, `devices` |
| `robot/state` | `protocol`, `events` | `actions`, `behavior` |
| `robot/safety` | `protocol`, `state` | `behavior`, `personality`, anything LLM-facing |
| `robot/actions` | `protocol`, `state`, `safety`, `events` | `behavior`, `personality` |
| `robot/behavior` | `actions`, `state`, `personality`, `events` | `devices` internals |
| `robot/devices` | `protocol`, `events`, `state`, and `core/*` **lazily** | `behavior` |
| `plugins/cozmo_bridge` | `robot/*`, `core/*` | — (it is the seam) |
| **anything reachable from an LLM tool** | — | any motion primitive, any raw actuator symbol |

`safety` deliberately sits **below** `behavior` and `personality`: personality can influence
which action is chosen, never whether it is allowed.

---

## 6. Files to add and modify

### 6.1 New files — `robot/` (none exist yet)

```
main/xiaozhi-server/robot/
  __init__.py
  config.py                       loads data/.robot.yaml; never touches config/settings.py
  logging.py                      stdlib logging; NEVER setup_logging() at module scope
  protocol/{__init__,messages,actions,telemetry,capabilities}.py     Pydantic models
  events/{__init__,bus,types}.py                                     bounded, drop-oldest
  state/{__init__,world,robot_state,freshness}.py
  devices/{__init__,registry,capabilities,session_adapter,mcp_client}.py
  actions/{__init__,model,lifecycle,queue,resources,executor}.py
  safety/{__init__,policy,limits,watchdog,estop}.py
  behavior/{__init__,engine,scoring,behaviors/}.py
  personality/{__init__,traits,emotion}.py
  vision/{__init__,pipeline,tracking,coordinates}.py
  memory/{__init__,tiers,store}.py
  animation/{__init__,player,library/*.yaml}                         data-driven, no Python per animation
  api/{__init__,server,routes,ws}.py                                 own aiohttp app, own port
  simulator/{__init__,device,scenarios/}.py
  tests/                                                             mirrors the tree
```

### 6.2 New files — the bridge

```
main/xiaozhi-server/plugins/cozmo_bridge/__init__.py    boot + register + SELF-ASSERT
main/xiaozhi-server/plugins/cozmo_bridge/tools.py       @register_function(..., IOT_CTL)
```

Constraints on the bridge, each traced to a finding:

* Must not re-export any `BasePlugin` subclass — `register_plugins_to_conn` reflects over
  `dir(module)` and registers duplicates with no dedup (R12).
* Must log its own failures loudly and assert that the expected tool names are present in
  `all_function_registry` and that `message_registry.get_handler("robot")` is not `None` —
  `scan_plugins` swallows import errors (R12).
* Must use `if TYPE_CHECKING:` for `ConnectionHandler` — at `connection.py:87` the class is
  not yet defined (`:116`), so a runtime import fails and is silently swallowed.
* Must not submit work to `conn.executor` — 5 workers already carry the whole blocking chat
  turn and become `None` during `close()` (`connection.py:1696`).

### 6.3 Modified upstream files — the whole budget

| File | Change | Finding |
|---|---|---|
| `core/connection.py` | +1 line after `:264`, +1 in the `finally` at `:308`, both `try/except` | R1 |
| `app.py` | +3 lines: create the robot control-plane task, add a done-callback that escalates, cancel it in the `finally` | R1, R12 |
| `core/providers/tools/device_mcp/mcp_client.py` | `:20` `next_id = 100` | R10 |
| `core/providers/tools/device_mcp/mcp_handler.py` | `:221-224` dispatch `notifications/*` | §2.5 |
| `core/api/vision_handler.py` | `:36-39` delete the auth bypass | R11 |

### 6.4 Modified non-upstream files — done in this change

See §7.

### 6.5 Reuse, do not duplicate

| Reuse | Where | Why not rebuild |
|---|---|---|
| Transport, handshake, auth | `core/websocket_server.py:71-152,214-235`, `core/auth.py` | The `<sig>.<ts>` HMAC scheme is reimplemented bit-for-bit in Java (`DeviceServiceImpl.java:608-633`); diverging breaks the console. |
| Opus decode/encode + 60 ms pacing | `connection.py:575-594`, `core/utils/opus_encoder_utils.py`, `core/utils/audioRateController.py`, `sendAudioHandle.py` | Hard-won hardware calibration (`PRE_BUFFER_COUNT=0` exists because ESP32-C3 buffers underrun). Reimplementing resurrects stuttering audio. |
| VAD + all 15 ASR providers | `core/providers/{vad,asr}/` | Free upstream fixes. Per-connection VAD state already lives on `conn`. |
| Device MCP client + `call_mcp_tool` | `device_mcp/mcp_handler.py:296` | Framing, envelope, de-sanitization, unwrapping, future cleanup — all done. |
| Tool schema delivery and result routing | `unified_tool_manager.py:49-60` → `connection.py:1146-1177` → `:1440-1546` | Multi-call handling, reporting and the REQLLM loop come free. |
| `Action` / `ActionResponse` | `plugins/register.py:40-57` | The tool return contract. |
| `tts_one_sentence` / `tts_start` / `tts_end` | `tts/base.py:318` | The robot's speech API. Spontaneous speech needs four steps, not one: fresh `conn.sentence_id`, `client_abort = False`, `tts_one_sentence(...)`, then `tts_end(conn)` — `tts_one_sentence` alone emits only `SentenceType.MIDDLE`, may produce no audio at all, and never sends `tts/stop`, leaving the device stuck speaking. |
| Provider factories | `core/utils/{llm,tts,asr,vad,vllm,memory,intent}.py` | Zero-edit provider registration by filename. Add files, never edit a registry. |
| `merge_configs`, `get_project_dir` | `config/config_loader.py:150-177`, `:15-17` | Unit-tested, pure, non-mutating. |
| `MarkdownCleaner`, `textUtils.check_emoji` | `core/utils/textUtils.py` | Output scrubbing before synthesis. |
| `cache_manager` | `core/utils/cache/manager.py:216` | **Always** with `namespace="robot"`. The unnamespaced `CacheType.CONFIG` dict holds `main_config` with FIFO eviction at 20 entries — overflowing it evicts the global config and the next `setup_logging()` crashes. |
| `BaseHandler` | `core/api/base_handler.py` | Subclass for CORS/OPTIONS consistency (new file, zero edit). Do **not** copy the `return` -inside-`finally` idiom from `ota_handler.py:351` / `vision_handler.py:156` — it swallows exceptions and `CancelledError`. |
| The vendoring tooling | `.cozmo/sync-upstream.sh` | Correct as written: a `commit-tree` graft preserving a real merge base plus a compare-and-swap `update-ref`. Leave it alone. |

### 6.6 Own it — do not build on the upstream version

Per-device provider instances (R5) · session registry (R1) · executor and event loop (R7) ·
session liveness and keepalive (R2) · protocol versioning and acks (`textMessageProcessor.py:35`
drops unknown types silently, so firmware skew is otherwise invisible) · dialogue windowing
(R12) · emotion state (the upstream signal is emoji-scraped from the first content chunk,
fires at most once per turn, defaults to `happy`, and never fires on the `direct_answer`
path — `textUtils.py:84-106`, `connection.py:1244-1251`; treat it as one weak input) ·
robot config loading · persistence (`mem_local_short` does an unlocked read-modify-write of
one shared YAML from an unjoined daemon thread — `mem_local_short.py:126-133`) · HTTP client
(`ManageApiClient` is a global singleton another subsystem's `safe_close()` can disable,
after which every call silently returns `None` — `manage_api_client.py:174-189,207-210`) ·
watchdog and timing.

---

## 7. Tooling and test infrastructure established in this change

All in Cozmo-owned or new files, so zero merge cost. Ruff and mypy config live in **new**
files rather than in the vendored `pyproject.toml`, per `docs/upstream-strategy.md`.

| File | Change |
|---|---|
| `main/xiaozhi-server/.ruff.toml` | **New.** Repo-wide floor: bug-only rules (`E9`, `F63`, `F7`, `F82`, `F811`, `PLE`, `B002`, `B011`, `B018`), green on the vendored tree today. `models/` and `libs/` excluded as vendored third-party. Documents the second tier: `robot/.ruff.toml` extends this with the full style/typing set when `robot/` lands, so new code is held to a high bar without reformatting 30k lines of upstream Python. |
| `main/xiaozhi-server/mypy.ini` | **New.** `python_version = 3.12`, `files = robot`, strict for `robot.*`; `core.*`/`config.*`/`plugins*` are `ignore_errors` + `follow_imports = skip` so robot code does not inherit thousands of errors from untyped upstream. |
| `main/xiaozhi-server/requirements-dev.txt` | **New.** Pinned `pytest`, `pytest-asyncio`, `freezegun`, `ruff`, `mypy`, plus the four runtime packages the current tests actually import. CI previously pip-installed **unpinned** test deps while pinning everything else. |
| `main/xiaozhi-server/tests/plugins/test_register.py` | **New.** 20 tests over `plugins/register.py`: the `FunctionRegistry` constructor regression, register/unregister/lookup, the decorator's effect on both global registries, and the `Action`/`ToolType` numeric codes that `ServerPluginExecutor.execute` dispatches on. |
| `.github/workflows/test.yml` | Added a `lint` job (ruff always, mypy when `robot/` exists). Changed the Python job from `pytest tests/test_smoke.py` to `pytest -q` — **2 asserts → 65 tests**. Added `develop` to the push triggers, so the default branch and the upstream-merge resolution step are finally covered. Added a Python 3.12 job (see below). |
| `Makefile` | Added `lint`, `lint-python`, `typecheck`. |
| `.gitignore` | Added `.ruff_cache/`, `.mypy_cache/`. |
| `main/xiaozhi-server/plugins/register.py` | **Behavioural fix.** `FunctionRegistry.__init__` called an undefined `setup_logging()` (`:127`), so the class raised `NameError` on instantiation — while being publicly re-exported from `plugins_func/register.py:13` and `plugins_func/__init__.py:17`. It now uses the module-level `SimpleLogger`. Called out explicitly because it is the one behaviour change in an otherwise non-behavioural commit; it was required to make the lint gate green, and the class was previously unusable, so nothing can regress. |
| `main/xiaozhi-server/plugins/__init__.py`, `performance_tester/performance_tester_stream_tts.py` | Removed a duplicate `import sys` / `import asyncio` (ruff `F811` autofix). No behaviour change. |

**The Python version split.** `requirements.txt` pins `torch==2.2.2`, which has no cp312
wheel, so the full-dependency job stays on 3.10. New robot code targets 3.12+, so a second
job runs the same suite on 3.12 against `requirements-dev.txt` — the minimal slice the tests
actually import. Verified: 65 passed, 2 skipped on a clean 3.12 venv with only
`requirements-dev.txt` installed. Collapsing the two jobs requires unpinning torch; that is
Phase 0 of the roadmap.

**Not done here, deliberately:** the two module-level test skips are left in place. One is
legitimate (`test_instance_creators.py` — `core/utils/intent.py:6` calls `setup_logging()`
at import, which needs `data/.config.yaml`). The other (`test_loadplugins.py`) is
factually stale and the test passes without it, but deleting it changes what runs, and this
commit's mandate was non-behavioural tooling. Both are Phase 0 items.

---

## 8. Assumptions, and where the README is ahead of the code

Stated explicitly so nothing here is an undocumented assumption.

1. **`robot/` does not exist.** Nothing in §4–§6 is implemented. The seams in §4.2 were
   each verified against source, but no code uses them yet.
2. **The README describes the product, not the build.** Its Development Stack section
   (`README.md:743,757-758,821,827`) promises Python 3.12+, Ruff and mypy/pyright; before
   this change none of that was configured anywhere, and the runtime is still pinned to
   3.10 by `torch`. Its seven claimed test categories (`:830-838`) do not exist — the suite
   is 65 tests over utility functions. Three documents it references are absent:
   `docs/robot-getting-started.md`, `PROJECT_STATUS.md`, and
   `docs/superpowers/specs/2026-09-02-test-infrastructure-design.md` (cited by
   `tests/test_smoke.py:3`).
3. **Robot firmware is assumed to speak Xiaozhi MCP.** The whole actuation path (seam ②)
   assumes the device implements `McpServer::AddTool` for its robot capabilities. If it does
   not, the fallback is a second robot-owned WebSocket on its own port — not `type:"robot"`
   on port 8000, which inherits the bind gate.
4. **Multi-robot is assumed to mean manager-api mode.** That is the mode where R5's
   provider sharing bites. In single-device local-config mode, `_initialize_private_config_async`
   returns early (`connection.py:845-849`) and TTS is built per connection.
5. **`main/xiaozhi-server/CLAUDE.md` is stale and actively misleading.** It describes the
   project purely as the upstream voice assistant, never mentions the robot, the
   no-upstream-edits rule, `pytest`, `tests/` or the `Makefile`, tells agents to use
   `uv run`, and points at `test_mcp_functions.py` — a file that does not exist — in three
   places. Rewriting it is Phase 0.
6. **`listen.mode: "realtime"` is a no-op.** It is stored and never branched on; every
   consumer tests only `== "manual"`. Continuous streaming is unimplemented work, not a
   config flag.
7. **Server-side AEC does nothing on the direct path.** `conn.client_aec` gates real-time
   barge-in (`receiveAudioHandle.py:30-32`), but echo subtraction needs the per-frame
   timestamps that exist only in the `?from=mqtt_gateway` framing. A robot with speakers
   near its mics gets barge-in but no echo cancellation by default.
8. **Binary framing is fork-specific.** The fork ignores `Protocol-Version` and
   `hello.version` entirely and switches framing on the URL suffix
   (`connection.py:271`); its 16-byte header (`sendAudioHandle.py:107-117`) is **not**
   upstream's `BinaryProtocol2` despite the matching size. A device configured for v2 or v3
   talking directly to this fork is misparsed as raw Opus. We assume raw-Opus direct mode.
9. **`.cozmo/unmerged-prs/` is ~41 MB in git history**, dominated by a single 40 MB patch.
    It is reference-only and costs every clone and every CI checkout. Pruning it is a
    Phase 0 candidate.
