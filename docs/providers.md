# Providers

A **provider** is a swappable adapter behind one stage of the conversation pipeline. nilo-server
has seven pluggable stages — VAD, ASR, LLM, VLLM (vision), TTS, Memory, Intent — plus a tool
system whose executors are also kept under `core/providers/`.

Every adapter on this page is **Implemented**: it exists in the tree today and is reachable from
configuration. Every adapter is also **inherited** from the upstream snapshot the repository was
forked from (see [upstream.md](upstream.md)); Nilo has added no provider of its own so far. The
robot domain (actions, behaviour, world model, embodied memory, simulator, safety policy) has no
providers at all — only `robot/protocol/` exists. See [robot-architecture.md](robot-architecture.md)
and [robot-roadmap.md](robot-roadmap.md).

Related pages: [configuration.md](configuration.md) for the config file layering,
[audio.md](audio.md) for the audio path a VAD/ASR/TTS provider plugs into,
[mcp.md](mcp.md) for the MCP tool transports.

## How a provider is selected

Selection is two hops: `selected_module.<Kind>` names a **block** under `<Kind>:`, and that block's
`type` field names a **module file** under `core/providers/<kind>/`.

```mermaid
flowchart LR
    A["selected_module.ASR: FunASR"] --> B["ASR.FunASR block"]
    B --> C["type: fun_local"]
    C --> D["core/providers/asr/fun_local.py"]
    D --> E["ASRProvider(config, ...)"]
```

If a block has no `type` key, the block name itself is used as the type — which is why the Memory
and Intent blocks are named `nomem`, `powermem`, `function_call` and so on.

| Kind | Config section | Factory | Module resolved | Class instantiated |
|---|---|---|---|---|
| VAD | `VAD:` | `core/utils/vad.py:create_instance` | `core/providers/vad/<type>.py` | `VADProvider` |
| ASR | `ASR:` | `core/utils/asr.py:create_instance` | `core/providers/asr/<type>.py` | `ASRProvider` |
| LLM | `LLM:` | `core/utils/llm.py:create_instance` | `core/providers/llm/<type>/<type>.py` | `LLMProvider` |
| VLLM | `VLLM:` | `core/utils/vllm.py:create_instance` | `core/providers/vllm/<type>.py` | `VLLMProvider` |
| TTS | `TTS:` | `core/utils/tts.py:create_instance` | `core/providers/tts/<type>.py` | `TTSProvider` |
| Memory | `Memory:` | `core/utils/memory.py:create_instance` | `core/providers/memory/<type>/<type>.py` | `MemoryProvider` |
| Intent | `Intent:` | `core/utils/intent.py:create_instance` | `core/providers/intent/<type>/<type>.py` | `IntentProvider` |

Consequences of that mechanism, all worth knowing before debugging a provider:

* **Class names are fixed by convention.** Every module in a given directory defines the class in
  the last column above; the factory imports the module and calls that name. A new adapter that
  names its class anything else fails with `AttributeError` at startup. The single exception is
  `core/providers/tts/default.py:DefaultTTS`, which is never selected by config (see the TTS table).
* **The lookup is a filesystem check relative to the working directory**
  (`os.path.exists(os.path.join('core', 'providers', ...))`), so the server must be started from
  `main/nilo-server/`. A wrong `type` raises `ValueError: Unsupported <KIND> type: ...`.
* **Type values are case-sensitive filenames.** `type: AliBL` resolves to
  `core/providers/llm/AliBL/AliBL.py`; `alibl` would not resolve.
* **VAD, ASR, LLM, Memory and Intent are constructed once per process**, in
  `core/websocket_server.py:WebSocketServer.__init__` via
  `core/utils/modules_initialize.py:initialize_modules`. VAD, LLM, Memory and Intent are then
  used as-is by every connection; **ASR is shared only when its `interface_type` is `LOCAL`** — a
  remote ASR owns a socket and a receive thread, so each connection builds its own
  (`core/connection.py:ConnectionHandler._initialize_asr`).
  **TTS is always constructed per connection**
  (`core/connection.py:ConnectionHandler._initialize_tts`).
* Instances are memoised in the config cache keyed by the block's contents
  (`core/utils/modules_initialize.py`), so editing a block's values rebuilds the provider while
  leaving other kinds untouched.

### Default stack

`config.yaml` ships this selection:

| Stage | Block | `type` | Runs |
|---|---|---|---|
| VAD | `SileroVAD` | `silero` | Local (model vendored) |
| ASR | `FunASR` | `fun_local` | Local (model must be downloaded) |
| LLM | `ChatGLMLLM` | `openai` | Cloud, Zhipu AI, needs `api_key` |
| VLLM | `ChatGLMVLLM` | `openai` | Cloud, Zhipu AI, needs `api_key` |
| TTS | `EdgeTTS` | `edge` | Cloud, Microsoft, no key |
| Memory | `nomem` | `nomem` | No-op |
| Intent | `function_call` | `function_call` | Local, needs a tool-calling LLM |

## ASR

Directory `core/providers/asr/`. Every module defines `ASRProvider(ASRProviderBase)`; the shared
base is `core/providers/asr/base.py`. Each adapter sets `self.interface_type` to `LOCAL`, `STREAM`
(audio is forwarded to the vendor as it arrives) or `NON_STREAM` (the utterance is buffered, then
posted in one request); `core/providers/asr/dto/dto.py` defines the enum.

| Module | `type` | Config blocks | Interface | Runs | Vendor / region | Notable requirements |
|---|---|---|---|---|---|---|
| `fun_local.py` | `fun_local` | `FunASR` | LOCAL | Local | FunASR / SenseVoice, offline | `funasr`, `torch`, `torchaudio`; weights at `model_dir` (default `models/SenseVoiceSmall`, `model.pt` not tracked in the repo); logs an error below 2 GB system RAM (`psutil`) |
| `fun_server.py` | `fun_server` | `FunASRServer` | NON_STREAM | Self-hosted | FunASR runtime container you run | `host`, `port`, `is_ssl`, `api_key`; WebSocket client only, no local model |
| `sherpa_onnx_local.py` | `sherpa_onnx_local` | `SherpaASR` (sense_voice), `SherpaParaformerASR` (paraformer) | LOCAL | Local | sherpa-onnx, offline | `sherpa_onnx`, `modelscope`; downloads `model.int8.onnx` and `tokens.txt` into `model_dir` from the ModelScope repo `pengzhendong/sherpa-onnx-sense-voice-zh-en-ja-ko-yue` on first run |
| `vosk.py` | `vosk` | `VoskASR` | LOCAL | Local | Vosk, offline | `vosk` (requirements pin it with `sys_platform != "darwin"`, so it is not installed on macOS); `model_path` must already exist or init raises; recogniser is fixed at 16 kHz |
| `doubao.py` | `doubao` | `DoubaoASR` | NON_STREAM | Cloud | Volcengine speech (China) | `appid`, `access_token`, `cluster`; billed per request |
| `doubao_stream.py` | `doubao_stream` | `DoubaoStreamASR`, `DoubaoStreamASRV2` | STREAM | Cloud | Volcengine speech (China) | `appid`, `access_token`, `resource_id` (selects the duration or concurrency plan, and the 1.0 or seed-asr service) |
| `tencent.py` | `tencent` | `TencentASR` | NON_STREAM | Cloud | Tencent Cloud, `asr.tencentcloudapi.com` (China) | `appid`, `secret_id`, `secret_key` |
| `aliyun.py` | `aliyun` | `AliyunASR` | NON_STREAM | Cloud | Alibaba Cloud NLS (China) | `appkey` plus either a 24-hour `token` or `access_key_id`/`access_key_secret` (token minted against `nls-meta.cn-shanghai.aliyuncs.com`) |
| `aliyun_stream.py` | `aliyun_stream` | `AliyunStreamASR` | STREAM | Cloud | Alibaba Cloud NLS (China) | Same credentials plus a regional `host`, default `nls-gateway-cn-shanghai.aliyuncs.com` |
| `aliyunbl_stream.py` | `aliyunbl_stream` | `AliyunBLStreamASR` | STREAM | Cloud | Alibaba Cloud Bailian / DashScope Paraformer (China) | `api_key`, `model` (e.g. `paraformer-realtime-v2`), sample-rate and punctuation switches |
| `baidu.py` | `baidu` | `BaiduASR` | NON_STREAM | Cloud | Baidu speech (China) | `baidu-aip` package; `app_id`, `api_key`, `secret_key`, `dev_pid` |
| `openai.py` | `openai` | `OpenaiASR`, `GroqASR` | NON_STREAM | Cloud | OpenAI, or any compatible transcription endpoint (`GroqASR` points the same adapter at Groq) | `api_key`, `base_url`, `model_name` |
| `qwen3_asr_flash.py` | `qwen3_asr_flash` | `Qwen3ASRFlash` | NON_STREAM | Cloud | Alibaba Cloud Bailian / DashScope (China) | `dashscope` package; `api_key`, `model_name`, optional language-ID and ITN switches |
| `xunfei_stream.py` | `xunfei_stream` | `XunfeiStreamASR` | STREAM | Cloud | iFlytek (China) | `app_id`, `api_key`, `api_secret`; `domain`, `language`, `accent` |

Voiceprint identification is not an ASR provider: it is attached to whatever ASR instance is
selected by `core/utils/modules_initialize.py:initialize_voiceprint`, and reads the top-level
`voiceprint:` block.

## VAD

Directory `core/providers/vad/`, class `VADProvider(VADProviderBase)`.

| Module | `type` | Config block | Runs | Notable requirements |
|---|---|---|---|---|
| `silero.py` | `silero` | `SileroVAD` | Local | `onnxruntime`, `numpy`; loads `<model_dir>/src/silero_vad/data/silero_vad.onnx`, and `models/snakers4_silero-vad` is vendored in the repo, so this provider works out of the box |

Tunables read by `core/providers/vad/silero.py:VADProvider.__init__`: `threshold` (speech onset,
code default 0.5), `threshold_low` (speech end, code default 0.2) and `min_silence_duration_ms`
(code default 1000; `config.yaml` ships 200). Each frame decision is pushed into a 5-slot sliding
window, and onset needs at least 3 of those 5 to be speech (`frame_window_threshold = 3`).
Behaviour details are in [audio.md](audio.md).

## LLM

Directory `core/providers/llm/<type>/<type>.py`, class `LLMProvider(LLMProviderBase)`.

The **Tools** column matters when `selected_module.Intent` is `function_call`:
`core/providers/llm/base.py:LLMProviderBase.response_with_functions` has a default implementation
that streams plain text and always yields a `None` tool call, so an adapter that does not override
it silently never fires a tool.

| Module | `type` | Config blocks | Runs | Vendor / region | Tools | Notable requirements |
|---|---|---|---|---|---|---|
| `openai/openai.py` | `openai` | `AliLLM`, `DoubaoLLM`, `DeepSeekLLM`, `ChatGLMLLM`, `TCBAiLLM`, `VolcesAiGatewayLLM`, `LMStudioLLM` | Cloud (LM Studio: local) | Any OpenAI-compatible endpoint — the shipped blocks point at Alibaba DashScope, Volcengine Ark, DeepSeek, Zhipu AI, Tencent CloudBase, the Volcengine AI gateway and a local LM Studio server | Native | `openai` package; `api_key`, `model_name`, `base_url` or `url`; optional `temperature`, `max_tokens`, `top_p`, `frequency_penalty` |
| `ollama/ollama.py` | `ollama` | `OllamaLLM` | Local | Ollama on `base_url`, default `http://localhost:11434` | Native | Model pulled with `ollama pull` first; prepends a no-think directive for qwen3 models |
| `xinference/xinference.py` | `xinference` | `XinferenceLLM`, `XinferenceSmallLLM` | Self-hosted | Xinference on `base_url`, default `http://localhost:9997` | Native | Model launched in Xinference first |
| `gemini/gemini.py` | `gemini` | `GeminiLLM` | Cloud | Google Gemini | Native | `google-generativeai` package; `api_key`, `model_name`; optional `http_proxy`/`https_proxy`, which the adapter probes before use |
| `AliBL/AliBL.py` | `AliBL` | `AliAppLLM` | Cloud | Alibaba Cloud Bailian *application* API (China) | Falls back to plain text with a warning | `dashscope` package; `app_id`, `api_key`; `is_no_prompt` moves the prompt into the Bailian app; `ali_memory_id` is a shared, not per-user, memory |
| `dify/dify.py` | `dify` | `DifyLLM` | Cloud or self-hosted | Dify, default `https://api.dify.ai/v1` | Prompt-emulated | `api_key`; `mode` selects `chat-messages`, `workflows/run` or `completion-messages`; the local prompt is ignored, set it in Dify |
| `coze/coze.py` | `coze` | `CozeLLM` | Cloud | Coze, pinned to the China base URL by `cozepy.COZE_CN_BASE_URL` | Prompt-emulated | `cozepy` package; `bot_id`, `user_id`, `personal_access_token` |
| `fastgpt/fastgpt.py` | `fastgpt` | `FastgptLLM` | Cloud or self-hosted | FastGPT | None — logs an error and yields nothing | `base_url`, `api_key`, optional `variables`; pair with `Intent: nointent` |
| `langflow/langflow.py` | `langflow` | `LangflowLLM` | Cloud or self-hosted | Langflow, default `https://api.langflow.astra.datastax.com` | None — inherits the base fallback | `api_key`, `flow_id`, optional `tweaks`; only the latest user message is sent, so the server-side prompt and history are ignored |
| `homeassistant/homeassistant.py` | `homeassistant` | `HomeAssistant` | Self-hosted | Home Assistant conversation agent | None — logs an error and yields nothing | `base_url`, `agent_id`, `api_key`; pair with `Intent: nointent` |

"Prompt-emulated" means the adapter injects a tool-description prompt built by
`core/providers/llm/system_prompt.py` into the user message and parses the model's reply, rather
than using a native tool-call API.

## VLLM (vision)

Directory `core/providers/vllm/`, class `VLLMProvider(VLLMProviderBase)`. This is the model behind
the `/mcp/vision/explain` HTTP endpoint (`core/api/vision_handler.py`).

| Module | `type` | Config blocks | Runs | Vendor / region | Notable requirements |
|---|---|---|---|---|---|
| `openai.py` | `openai` | `ChatGLMVLLM`, `QwenVLVLLM`, `XunfeiSparkLLM` | Cloud | Zhipu AI `glm-4v-flash`; Alibaba DashScope `qwen3.5-flash`; see the note below | `openai` package; `model_name`, `api_key`, `url` or `base_url`; optional `max_tokens` (default 500), `temperature` (0.7), `top_p` (1.0). The image is sent inline as a base64 `data:image/jpeg` URL in a non-streaming chat completion |

`openai` is the only VLLM adapter in the tree, so every `VLLM:` block must use `type: openai`.
The `XunfeiSparkLLM` block is an inherited inconsistency: it is named for iFlytek but its
`base_url` is the Volcengine Ark endpoint and `model_name` is `lite`. Treat it as a sample entry,
not a working vision configuration.

## TTS

Directory `core/providers/tts/`, class `TTSProvider(TTSProviderBase)`, base
`core/providers/tts/base.py`. `interface_type` (`core/providers/tts/dto/dto.py`) is `NON_STREAM`
(one request per sentence, default from the base class), `SINGLE_STREAM` (response body consumed
as it arrives) or `DUAL_STREAM` (a persistent socket that takes text and returns audio
concurrently).

| Module | `type` | Config blocks | Interface | Runs | Vendor / region | Notable requirements |
|---|---|---|---|---|---|---|
| `edge.py` | `edge` | `EdgeTTS` | NON_STREAM | Cloud | Microsoft Edge read-aloud voices | `edge_tts` package; no API key; needs direct reachability of the Microsoft endpoint; `voice`, plus `volume`/`rate`/`pitch` |
| `doubao.py` | `doubao` | `DoubaoTTS`, `TTS302AI` | NON_STREAM | Cloud | Volcengine speech (China); `TTS302AI` points the same adapter at a 302.AI proxy URL | `appid`, `access_token`, `api_url`, `voice` |
| `huoshan_double_stream.py` | `huoshan_double_stream` | `HuoshanDoubleStreamTTS`, `HuoshanDoubleStreamTTSV2` | DUAL_STREAM | Cloud | Volcengine bidirectional TTS (China) | `ws_url`, `appid`, `access_token`, `resource_id`, `speaker`; credentials travel as WebSocket handshake headers; a cloned-voice id goes in `speaker` |
| `aliyun.py` | `aliyun` | `AliyunTTS` | NON_STREAM | Cloud | Alibaba Cloud NLS (China) | `appkey` plus `token` or access-key pair |
| `aliyun_stream.py` | `aliyun_stream` | `AliyunStreamTTS` | DUAL_STREAM | Cloud | Alibaba Cloud NLS (China) | Same credentials plus a regional `host`, default `nls-gateway-cn-beijing.aliyuncs.com` |
| `alibl_stream.py` | `alibl_stream` | `AliBLTTS` | DUAL_STREAM | Cloud | Alibaba Cloud Bailian / DashScope (China) | `api_key`, `ws_url`, `voice` |
| `tencent.py` | `tencent` | `TencentTTS` | NON_STREAM | Cloud | Tencent Cloud, `tts.tencentcloudapi.com` (China) | `appid`, `secret_id`, `secret_key` |
| `xunfei_stream.py` | `xunfei_stream` | `XunFeiTTS` | DUAL_STREAM | Cloud | iFlytek (China) | `app_id`, `api_key`, `api_secret`, `api_url` |
| `minimax_httpstream.py` | `minimax_httpstream` | `MinimaxTTSHTTPStream` | NON_STREAM | Cloud | MiniMax, `api.minimaxi.com` (China) | `group_id`, `api_key`, `voice_id` |
| `siliconflow.py` | `siliconflow` | `CosyVoiceSiliconflow` | NON_STREAM | Cloud | SiliconFlow, `api.siliconflow.cn` (China) | `access_token`, `model`, `voice` |
| `cozecn.py` | `cozecn` | `CozeCnTTS` | NON_STREAM | Cloud | Coze, `api.coze.cn` (China) | `access_token`, `voice` |
| `openai.py` | `openai` | `OpenAITTS`, `VolcesAiGatewayTTS` | NON_STREAM | Cloud | OpenAI `/v1/audio/speech`, or any compatible endpoint (`VolcesAiGatewayTTS` points it at the Volcengine AI gateway) | `api_key`, `api_url`, `model`, `voice` |
| `fishspeech.py` | `fishspeech` | `FishSpeech` | NON_STREAM | Self-hosted | fish-speech `api_server` | `ormsgpack`, `pydantic`; request body is msgpack; `api_url` default `http://127.0.0.1:8080/v1/tts`; optional reference audio for voice cloning, read from disk relative to the working directory |
| `gpt_sovits_v2.py` | `gpt_sovits_v2` | `GPT_SOVITS_V2` | NON_STREAM | Self-hosted | GPT-SoVITS v2 | `url` default `http://127.0.0.1:9880/tts` |
| `gpt_sovits_v3.py` | `gpt_sovits_v3` | `GPT_SOVITS_V3` | NON_STREAM | Self-hosted | GPT-SoVITS v3 | `url` default `http://127.0.0.1:9880` |
| `paddle_speech.py` | `paddle_speech` | `PaddleSpeechTTS` | NON_STREAM | Self-hosted | PaddleSpeech streaming server | `websockets`, `numpy`; only `protocol: websocket` is implemented — any other value raises `ValueError` at synthesis time |
| `index_stream.py` | `index_stream` | `IndexStreamTTS` | SINGLE_STREAM | Self-hosted | index-tts-vllm | `api_url` must return raw 16-bit little-endian PCM at 24 kHz mono; `voice` must be a speaker registered on that server |
| `custom.py` | `custom` | `CustomTTS` | NON_STREAM | Anything | Your own HTTP endpoint | `url`, `method`, `headers`, `params` (a dict, or a JSON string; `{prompt_text}` inside a value is substituted with the sentence), `format` |
| `default.py` | — | — | NON_STREAM | — | — | Not selectable. `DefaultTTS` is the fallback `core/connection.py:ConnectionHandler._initialize_tts` installs when TTS initialisation returns nothing; every synthesis call logs an error instead of producing audio |

Regardless of adapter, produced audio is decoded with `pydub` (an `ffmpeg` subprocess) and
re-encoded to Opus, so a missing `ffmpeg` or Opus library shows up as a TTS failure rather than a
provider error. See [audio.md](audio.md).

## Memory

Directory `core/providers/memory/<type>/<type>.py`, class `MemoryProvider(MemoryProviderBase)`.

| Module | `type` | Config block | Runs | Vendor / region | Notable requirements |
|---|---|---|---|---|---|
| `nomem/nomem.py` | `nomem` | `nomem` | — | — | Memory disabled: `save_memory` and `query_memory` are logged no-ops. This is the default |
| `mem_local_short/mem_local_short.py` | `mem_local_short` | `mem_local_short` | Local | None — summaries are produced by an LLM you already configured | Optional `llm:` names another `LLM:` block to do the summarising; summaries are written to `data/.memory.yaml` |
| `mem_report_only/mem_report_only.py` | `mem_report_only` | none shipped | — | — | No-op like `nomem`; it exists for the inherited reporting path and has no block in `config.yaml`, so selecting it means adding one |
| `mem0ai/mem0ai.py` | `mem0ai` | `mem0ai` | Cloud | Mem0 hosted service | `mem0ai` package; `api_key`. A rejected key disables the provider rather than failing the connection |
| `powermem/powermem.py` | `powermem` | `powermem` | Local or cloud, depending on the stores you pick | PowerMem (OceanBase) with an LLM, an embedding model and a vector store you choose | `powermem` package; nested `llm`, `embedder` and `vector_store` blocks (`sqlite` needs no extra settings, `oceanbase`/`seekdb`/`postgres` do); `enable_user_profile: true` switches to profile memory. Any initialisation failure degrades to no memory instead of breaking conversations |

## Intent

Directory `core/providers/intent/<type>/<type>.py`, class `IntentProvider(IntentProviderBase)`.
This stage decides whether an utterance is plain chat or a tool call.

| Module | `type` | Config block | Notable requirements |
|---|---|---|---|
| `nointent/nointent.py` | `nointent` | `nointent` | Always returns `continue_chat`; no tools ever fire |
| `function_call/function_call.py` | `function_call` | `function_call` | Also always returns `continue_chat` — tool selection is delegated to the LLM's native tool calling, so the selected LLM must implement `response_with_functions` (see the LLM table). This is the default |
| `intent_llm/intent_llm.py` | `intent_llm` | `intent_llm` | Runs a separate classification call before the main turn: slower, but works with any LLM. Optional `llm:` names another `LLM:` block; uses the last 4 dialogue turns and caches results |

Both `function_call` and `intent_llm` read a `functions:` list from their own block. That list is
the set of server plugins offered to the model.

## Tools

Tools are not selected by `selected_module`. They are gathered per connection by
`core/providers/tools/unified_tool_handler.py:UnifiedToolHandler`, which owns a
`core/providers/tools/unified_tool_manager.py:ToolManager` and registers one executor per
`ToolType` (`core/providers/tools/base/tool_types.py`).

| `ToolType` | Executor | Module | Where the tools come from |
|---|---|---|---|
| `SERVER_PLUGIN` | `ServerPluginExecutor` | `core/providers/tools/server_plugins/plugin_executor.py` | Python functions in `plugins_func/functions/` registered with `@register_function` |
| `SERVER_MCP` | `ServerMCPExecutor` | `core/providers/tools/server_mcp/mcp_executor.py` | MCP servers the backend launches or connects to, configured in `data/.mcp_server_settings.json` (`mcp_server_settings.json` at the repo is the annotated sample); stdio, SSE and streamable-HTTP transports |
| `DEVICE_IOT` | `DeviceIoTExecutor` | `core/providers/tools/device_iot/iot_executor.py` | Capability descriptors the device sends over the session; turned into tools at runtime |
| `DEVICE_MCP` | `DeviceMCPExecutor` | `core/providers/tools/device_mcp/mcp_executor.py` | An MCP server running on the device itself, spoken to over the session socket |
| `MCP_ENDPOINT` | `MCPEndpointExecutor` | `core/providers/tools/mcp_endpoint/mcp_endpoint_executor.py` | A remote MCP endpoint given by the top-level `mcp_endpoint:` config value |

Details of the three MCP paths are in [mcp.md](mcp.md).

### Server plugins

Modules in `plugins_func/functions/`, imported at handler init by `auto_import_modules`. The
`ToolType` in the table below is the plugin-level enum from `plugins/register.py` (re-exported by
`plugins_func/register.py`), which tells the executor how to call the function — `SYSTEM_CTL` and
`IOT_CTL` receive the connection object, `WAIT` does not, `CHANGE_SYS_PROMPT` swaps the persona.

| Tool name | Module | `ToolType` | What it calls | Region notes |
|---|---|---|---|---|
| `handle_exit_intent` | `plugins_func/functions/handle_exit_intent.py` | `SYSTEM_CTL` | Nothing; sets `close_after_chat` on the connection | — |
| `get_lunar` | `plugins_func/functions/get_time.py` | `WAIT` | Nothing; local `cnlunar` computation | Chinese lunar calendar and almanac; answers are China-specific by nature |
| `play_music` | `plugins_func/functions/play_music.py` | `SYSTEM_CTL` | Local files under the `music_dir` of the `play_music` entry in the `plugins:` block | — |
| `change_role` | `plugins_func/functions/change_role.py` | `CHANGE_SYS_PROMPT` | Nothing; swaps the system prompt for one of three built-in personas | — |
| `get_weather` | `plugins_func/functions/get_weather.py` | `SYSTEM_CTL` | QWeather GeoAPI, then scrapes the returned forecast page with BeautifulSoup | China-oriented: the geo lookup is issued with a Chinese language parameter, and the code defaults are a shared QWeather host, a shared key and a Chinese default city. Set `api_host`, `api_key` and `default_location` under the `get_weather` entry of the `plugins:` block |
| `get_news_from_newsnow` | `plugins_func/functions/get_news_from_newsnow.py` | `SYSTEM_CTL` | The NewsNow aggregator at the `url` prefix of the `get_news_from_newsnow` entry in the `plugins:` block | Chinese sources only: `news_sources` must be exact Chinese source names from the module's `CHANNEL_MAP`, separated by `;`; an unmatched name falls back to one default source |
| `get_news_from_chinanews` | `plugins_func/functions/get_news_from_chinanews.py` | `SYSTEM_CTL` | RSS feeds configured under the `get_news_from_chinanews` entry of the `plugins:` block | Chinese-language news feeds |
| `web_search` | `plugins_func/functions/web_search.py` | `SYSTEM_CTL` | Metaso (China) or Tavily, chosen by the `provider` key of the `web_search` entry in the `plugins:` block | Metaso is a Chinese search API; Tavily is the international option. A missing key or unknown provider returns a configuration message to the model instead of raising. Code default for `max_results` is 3, while `config.yaml` ships 5 |
| `search_from_ragflow` | `plugins_func/functions/search_from_ragflow.py` | `SYSTEM_CTL` | A self-hosted RAGFlow retrieval API | — |
| `hass_get_state`, `hass_set_state` | `plugins_func/functions/hass_state.py` | `SYSTEM_CTL` | Home Assistant REST API | — |
| `hass_play_music` | `plugins_func/functions/hass_play_music.py` | `SYSTEM_CTL` | Home Assistant Music Assistant service | Mutually exclusive with `play_music`; enable one |

`plugins_func/functions/hass_init.py` registers no tool — it concatenates the configured Home
Assistant devices into the system prompt at handler init.

Two rules about the `functions:` list that are easy to get wrong:

* **Listing a module name expands to every tool it registers.** `hass_state` expands to
  `hass_get_state` and `hass_set_state`, via `module_func_map` in `plugins/register.py`.
* **Two tools are always loaded** whether or not they are listed: `handle_exit_intent` and
  `get_lunar` (`necessary_functions` in `core/providers/tools/server_plugins/plugin_executor.py`).
  The comment in `config.yaml` that names `play_music` as always-on is stale — `play_music` is
  loaded only because the shipped `functions:` lists include it.

A tool's LLM-facing description can be overridden from config: `plugins.<tool or module>.description`
replaces it when tools are collected, which is how `search_from_ragflow` and `web_search` are
told what your knowledge base or search scope contains.

## What the tests exercise

`make test` (equivalently `cd main/nilo-server && python -m pytest -q`) runs 126 tests in about a
second. **No test opens a network connection, and none needs a provider credential.** Provider
coverage is deliberately thin:

| Test | What it covers |
|---|---|
| `tests/core/utils/test_instance_creators.py` | The `create_instance` factories for Intent, LLM and Memory: an unknown name raises `ValueError`, and the signature accepts `*args`/`**kwargs`. No provider is constructed |
| `tests/test_imports.py` | Imports every module under `config`, `core`, `plugins`, `plugins_func` and `robot`. Note that `core/providers/` has no `__init__.py`, so `pkgutil.walk_packages` does **not** descend into it: adapter modules are not individually imported. What is imported transitively through `core/connection.py` is `core/providers/tts/base.py`, `core/providers/tts/default.py`, `core/providers/asr/dto/dto.py` and the whole of `core/providers/tools/` |
| `tests/plugins/test_register.py` | `FunctionRegistry` construction, register, unregister and lookup — the plugin registry every server plugin depends on |
| `tests/plugins_func/test_loadplugins.py` | That the auto-import side effect still loads `plugins_func/functions/`; skipped when the full runtime dependencies are absent |

So a broken vendor adapter will not be caught by CI. The tools for checking one by hand are the
benchmark scripts in `performance_tester/`, which load the same config sections the server uses
(see [testing.md](testing.md) and [development.md](development.md)).

## Adding a provider

1. Create `core/providers/<kind>/<type>.py` (or `core/providers/<kind>/<type>/<type>.py` for LLM,
   Memory and Intent) and define the class name from the selection table — `ASRProvider`,
   `TTSProvider`, `LLMProvider`, `VLLMProvider`, `VADProvider`, `MemoryProvider` or
   `IntentProvider` — subclassing that directory's `base.py`.
2. Add a block under the matching section of `data/.config.yaml` with `type: <type>` and whatever
   keys your adapter reads, and point `selected_module.<Kind>` at the block name.
3. For ASR and TTS, set `self.interface_type` so the audio pipeline knows whether to stream.
4. For an LLM that should drive tools, override `response_with_functions`; without it the base
   class silently streams text and never emits a tool call.
5. Keep any new third-party dependency in `requirements.txt`, and remember that `tests/test_imports.py`
   will not import your module — add a test if it has logic worth pinning.
