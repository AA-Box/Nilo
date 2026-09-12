import os
import asyncio
import yaml
from collections.abc import Mapping
from loguru import logger
from config.manage_api_client import (
    init_service,
    get_server_config,
    get_agent_models,
    get_correct_words,
    DeviceNotFoundException,
    DeviceBindException,
)


# Environment variables that override config keys (product boundary; see docs/configuration.md).
ENV_OVERRIDES = {
    "KIVO_SERVER_HOST": ("server", "ip"),
    "KIVO_SERVER_PORT": ("server", "port"),
    "KIVO_HTTP_PORT": ("server", "http_port"),
    "KIVO_LOG_LEVEL": ("log", "log_level"),
}
_INT_KEYS = {"port", "http_port"}

# Old top-level config keys that still load under their new name, with a warning.
DEPRECATED_KEYS = {"xiaozhi": "hello"}


def get_project_dir():
    """Absolute path of the server directory, with trailing slash."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def custom_config_path():
    """Path of the user override file: $KIVO_CONFIG or data/.config.yaml."""
    return os.environ.get("KIVO_CONFIG") or get_project_dir() + "data/.config.yaml"


def apply_deprecated_aliases(config):
    """Move deprecated top-level keys to their new names (in place)."""
    for old, new in DEPRECATED_KEYS.items():
        if old not in config:
            continue
        if new in config:
            logger.warning(f"config key '{old}' is deprecated and ignored because '{new}' is also set")
            config.pop(old)
        else:
            logger.warning(f"config key '{old}' is deprecated; rename it to '{new}'")
            config[new] = config.pop(old)
    return config


def apply_env_overrides(config, environ=None):
    """Apply KIVO_* environment overrides (in place). Env wins over both config files."""
    environ = os.environ if environ is None else environ
    for env_name, (section, key) in ENV_OVERRIDES.items():
        raw = environ.get(env_name)
        if raw is None or raw == "":
            continue
        section_map = config.setdefault(section, {})
        section_map[key] = int(raw) if key in _INT_KEYS else raw
    return config


def read_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config


async def load_config():
    """Load the configuration."""
    from core.utils.cache.manager import cache_manager, CacheType

    # Check the cache
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_path = custom_config_path()

    # aliases are applied per layer so a user override written with the old key
    # still overrides the shipped block instead of being ignored next to it
    default_config = apply_deprecated_aliases(read_config(default_config_path))
    custom_config = apply_deprecated_aliases(read_config(custom_path) or {})

    if custom_config.get("manager-api", {}).get("url"):
        config = apply_deprecated_aliases(await get_config_from_api_async(custom_config))
    else:
        config = merge_configs(default_config, custom_config)
    apply_env_overrides(config)
    ensure_directories(config)

    # Cache the config
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


async def get_config_from_api_async(config):
    """Fetch the config from the Java API (async)."""
    # Initialise the API client
    init_service(config)

    # Fetch the server config
    config_data = await get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    auth_enabled = config_data.get("server", {}).get("auth", {}).get("enabled", False)
    # The server section always comes from the local config
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    config_data["server"]["auth"] = {"enabled": auth_enabled}
    # Fall back to the local prompt_template if the API did not return one
    if not config_data.get("prompt_template"):
        config_data["prompt_template"] = config.get("prompt_template")
    return config_data


async def get_private_config_from_api(config, device_id, client_id):
    """Fetch the device's private config from the Java API."""
    results = await asyncio.gather(
        get_agent_models(device_id, client_id, config["selected_module"]),
        get_correct_words(device_id),
        return_exceptions=True,
    )
    agent_result = results[0]
    correct_words = results[1] if not isinstance(results[1], Exception) else None

    # Re-raise business exceptions
    if isinstance(agent_result, DeviceNotFoundException):
        raise agent_result
    if isinstance(agent_result, DeviceBindException):
        raise agent_result

    private_config = agent_result if not isinstance(agent_result, Exception) else {}
    if correct_words:
        private_config["correct_words"] = correct_words
    return private_config


def ensure_directories(config):
    """Make sure every directory referenced by the config exists."""
    dirs_to_create = set()
    project_dir = get_project_dir()  # project root
    # Log directory
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS provider output directories
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # Output directories of the selected providers
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module_type) is None:
            continue
        if config.get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # Create all directories in one pass
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"Warning: cannot create directory {dir_path}; check write permissions")


def merge_configs(default_config, custom_config):
    """
    Recursively merge two configs; custom_config takes precedence.

    Args:
        default_config: shipped default config
        custom_config: user override config

    Returns:
        The merged config.
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged
