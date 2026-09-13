import yaml
import os
from .base import BasePlugin, PluginAction


class PluginConfig:
    """Plugin configuration"""

    def __init__(self, name, enabled=True, priority=100, config=None):
        self.name = name
        self.enabled = enabled
        self.priority = priority
        self.config = config or {}


class PluginManager:
    """Plugin manager - supports priorities, enable switches and three-state results"""

    def __init__(self, config_path=None):
        self.plugins = []
        self.plugin_configs = {}
        self.config_path = config_path

        if config_path and os.path.exists(config_path):
            self._load_plugin_configs_from_main()

    def _load_plugin_configs_from_main(self):
        """Load dynamic interceptor configs from the main config"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            for name, cfg in config.get('dynamic_interceptors', {}).items():
                self.plugin_configs[name] = PluginConfig(
                    name=name,
                    enabled=cfg.get('enabled', True),
                    priority=cfg.get('priority', 100),
                    config=cfg
                )
        except Exception as e:
            print(f"Failed to load plugin config: {e}")

    def register_plugin(self, plugin, config=None):
        """Register a plugin"""
        if not isinstance(plugin, BasePlugin):
            return

        plugin_name = plugin.name if hasattr(plugin, 'name') else plugin.__class__.__name__

        if config:
            plugin_config = PluginConfig(
                name=plugin_name,
                enabled=config.get('enabled', True),
                priority=config.get('priority', 100),
                config=config
            )
        elif plugin_name in self.plugin_configs:
            plugin_config = self.plugin_configs[plugin_name]
        else:
            plugin_config = PluginConfig(name=plugin_name, enabled=True, priority=100)

        plugin._plugin_config = plugin_config

        if plugin_config.enabled:
            self.plugins.append(plugin)
            self.plugins.sort(key=lambda p: p._plugin_config.priority if hasattr(p, '_plugin_config') else 100)

    def load_plugins_from_config(self, config_path, conn):
        """Load plugins from a config file"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                plugin_config = yaml.safe_load(f)

            for plugin_info in plugin_config.get('plugins', []):
                plugin_type = plugin_info.get('type')
                enabled = plugin_info.get('enabled', True)
                priority = plugin_info.get('priority', 100)

                if not enabled or not plugin_type:
                    continue

                try:
                    module_name, class_name = plugin_type.rsplit('.', 1)
                    import importlib
                    module = importlib.import_module(module_name)
                    plugin_class = getattr(module, class_name)
                    plugin = plugin_class(logger=conn.logger if hasattr(conn, 'logger') else None)
                    self.register_plugin(plugin, {'enabled': enabled, 'priority': priority, 'config': plugin_info})
                except Exception as e:
                    print(f"Failed to load plugin {plugin_type}: {e}")
        except Exception as e:
            print(f"Failed to load plugin config: {e}")

    async def process_text(self, conn, text):
        """Process text - supports three-state results (RELEASE/INTERCEPT/CLOSE)

        Returns:
            tuple: (result, action)
                - result: processed text or response message
                - action: PluginAction enum value
        """
        processed_text = text

        for plugin in self.plugins:
            try:
                result = await plugin.pre_process_text(conn, processed_text)

                # Support the old format (result, bool) and the new format (result, PluginAction)
                if isinstance(result, tuple) and len(result) == 2:
                    if isinstance(result[1], bool):  # old format
                        action = PluginAction.INTERCEPT if result[1] else PluginAction.RELEASE
                        processed_text, action = result[0], action
                    else:  # new format
                        processed_text, action = result
                else:
                    # Unexpected format, skip
                    continue

                # Handle each state and set the connection flag
                if action == PluginAction.CLOSE:
                    conn.close_after_chat = True
                    return processed_text, PluginAction.CLOSE
                elif action == PluginAction.INTERCEPT:
                    conn.close_after_chat = False
                    return processed_text, PluginAction.INTERCEPT
                # RELEASE: continue to the next plugin, leave the flag alone

            except Exception as e:
                if hasattr(conn, 'logger'):
                    conn.logger.error(f"Plugin {plugin.name} failed: {e}")
                continue

        return processed_text, PluginAction.RELEASE

    def get_plugins_info(self):
        """Get info for all plugins"""
        result = []
        for plugin in self.plugins:
            info = plugin.get_info()
            if hasattr(plugin, '_plugin_config'):
                info['enabled'] = plugin._plugin_config.enabled
                info['priority'] = plugin._plugin_config.priority
            result.append(info)
        return result

    def get_all_plugins_count(self):
        """Get the total plugin count"""
        return len(self.plugin_configs) if self.plugin_configs else len(self.plugins)

    def is_plugin_enabled(self, plugin_name):
        """Check whether a plugin is enabled"""
        if plugin_name in self.plugin_configs:
            return self.plugin_configs[plugin_name].enabled
        return any(hasattr(p, 'name') and p.name == plugin_name for p in self.plugins)