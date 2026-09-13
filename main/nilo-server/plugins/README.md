# Unified plugin system

## Overview

`plugins/` provides one scanning and registration mechanism for two kinds of plugins:

1. **Interceptor plugins** - subclasses of `BasePlugin`; they pre-process the user's text before it reaches the LLM and may intercept it.
2. **MCP function plugins** - tool functions registered with the `@register_function` decorator.

A single plugin directory may contain both.

## Directory layout

```
plugins/
├── __init__.py              # scan_plugins() and register_plugins_to_conn()
├── base.py                  # BasePlugin base class and PluginAction enum
├── manager.py               # PluginManager (per-connection plugin list, priorities)
├── register.py              # @register_function decorator, ToolType, Action, ActionResponse, registries
├── loadplugins.py           # auto_import_modules() helper
│
├── functions/               # optional: flat legacy layout, imported with auto_import_modules() if present
│
└── my_plugin/             # one plugin per subdirectory; needs an __init__.py
    ├── __init__.py          # plugin class and/or @register_function tools
    └── config.yaml          # optional: the plugin's own config files and helper modules
```

`scan_plugins()` treats every subdirectory that has an `__init__.py` as a plugin, except names starting with `_` or `.` and the `functions/` directory.

The tools shipped with the server (`get_time`, `get_weather`, `play_music`, ...) still live in `plugins_func/functions/` and are loaded separately by `core/connection.py` via `auto_import_modules("plugins_func.functions")`.

## Writing plugins

### Interceptor plugin

```python
# plugins/my_plugin/__init__.py
from plugins.base import BasePlugin, PluginAction

class MyInterceptor(BasePlugin):
    def __init__(self, logger=None):
        self.name = "MyInterceptor"
        self.description = "My interceptor plugin"
        self.logger = logger

    async def pre_process_text(self, conn, text):
        # return (processed text or reply, action)
        if "special command" in text:
            return "Done", PluginAction.CLOSE
        return text, PluginAction.RELEASE
```

`register_plugins_to_conn()` instantiates every `BasePlugin` subclass it finds as `cls(logger=conn.logger)`, so the constructor must accept a `logger` keyword. `BasePlugin.speak(conn, text)` sends a spoken reply through TTS.

### MCP function plugin

```python
# plugins/my_plugin/__init__.py
from plugins.register import register_function, ToolType, ActionResponse, Action

@register_function("get_time", {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current time",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}, ToolType.WAIT)
async def get_time():
    return ActionResponse(Action.RESPONSE, "The current time is...", None)
```

`ToolType` values: `NONE`, `WAIT`, `CHANGE_SYS_PROMPT`, `SYSTEM_CTL`, `IOT_CTL`, `MCP_CLIENT`.
`Action` values: `ERROR`, `NOTFOUND`, `NONE`, `RESPONSE` (reply directly), `REQLLM` (let the LLM phrase the reply), `RECORD`.

### Mixed plugin (recommended)

```python
# plugins/my_plugin/__init__.py
from plugins.base import BasePlugin, PluginAction
from plugins.register import register_function, ToolType, ActionResponse, Action
from .config import load_config  # optional: the plugin's own config

# 1. Interceptor logic
class MyPlugin(BasePlugin):
    def __init__(self, logger=None):
        self.name = "MyPlugin"
        self.description = "My mixed plugin"
        self.logger = logger
        self.config = load_config()

    async def pre_process_text(self, conn, text):
        return text, PluginAction.RELEASE

# 2. MCP functions
@register_function("my_tool", {...}, ToolType.WAIT)
async def my_tool(param):
    return ActionResponse(Action.REQLLM, "result", None)
```

## How plugins are loaded

`core/connection.py` calls `scan_plugins()` once at import time. Each `ConnectionHandler` creates its own `PluginManager()` in `__init__` and calls `register_plugins_to_conn(self)` in `handle_connection()` after authentication:

```python
from plugins.manager import PluginManager
from plugins import scan_plugins, register_plugins_to_conn

scan_plugins()  # once per process

class ConnectionHandler:
    def __init__(self, ...):
        self.plugin_manager = PluginManager()

    async def handle_connection(self, ws):
        register_plugins_to_conn(self)
        ...
```

### Execution flow

```
user speech → core/handle/receiveAudioHandle.py → conn.plugin_manager.process_text(conn, text)
                                                    ↓
                                          plugin 1.pre_process_text()   (lowest priority value first)
                                                    ↓
                                          plugin 2.pre_process_text()
                                                    ↓
                                          ... → returns (text, action)
```

`process_text()` stops at the first plugin that returns `INTERCEPT` or `CLOSE`; on `RELEASE` the (possibly modified) text is handed to the next plugin. Legacy `(text, bool)` return values are still accepted (`True` maps to `INTERCEPT`).

## PluginAction enum

- `RELEASE` - pass through; the text continues into the normal chat flow
- `INTERCEPT` - stop; `receiveAudioHandle` speaks the returned text via TTS and skips the LLM
- `CLOSE` - like `INTERCEPT`, and sets `conn.close_after_chat = True` so the connection is closed afterwards

## Priorities and enabling

`PluginManager(config_path=...)` can read a `dynamic_interceptors:` mapping (`enabled`, `priority` per plugin name) from a YAML file; lower priority values run first. The server constructs `PluginManager()` without a path, so every discovered plugin is enabled with priority 100.

## Backward compatibility

Legacy import paths still work:

- `plugins_func.register` → re-exports `plugins.register`
- `plugins_func.loadplugins` → re-exports `plugins.loadplugins`
- `plugins_func` → re-exports the registry classes from `plugins.register`

## Testing

```bash
# plugin scan
python -c "
from plugins import scan_plugins
scan_plugins()
print('plugin scan complete')
"

# MCP function registration
python -c "
from plugins import scan_plugins
from plugins.register import all_function_registry
scan_plugins()
print('registered functions:', list(all_function_registry.keys()))
"

# interceptor registration
python -c "
from plugins import scan_plugins, register_plugins_to_conn
from plugins.manager import PluginManager

class MockConn:
    def __init__(self):
        self.plugin_manager = PluginManager()

scan_plugins()
conn = MockConn()
register_plugins_to_conn(conn)
print(f'registered plugins: {len(conn.plugin_manager.plugins)}')
"
```
