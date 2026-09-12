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
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
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

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


async def get_config_from_api_async(config):
    """从Java API获取配置（异步版本）"""
    # 初始化API客户端
    init_service(config)

    # 获取服务器配置
    config_data = await get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    auth_enabled = config_data.get("server", {}).get("auth", {}).get("enabled", False)
    # server的配置以本地为准
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    config_data["server"]["auth"] = {"enabled": auth_enabled}
    # 如果服务器没有prompt_template，则从本地配置读取
    if not config_data.get("prompt_template"):
        config_data["prompt_template"] = config.get("prompt_template")
    return config_data


async def get_private_config_from_api(config, device_id, client_id):
    """从Java API获取私有配置"""
    results = await asyncio.gather(
        get_agent_models(device_id, client_id, config["selected_module"]),
        get_correct_words(device_id),
        return_exceptions=True,
    )
    agent_result = results[0]
    correct_words = results[1] if not isinstance(results[1], Exception) else None

    # 抛出业务异常
    if isinstance(agent_result, DeviceNotFoundException):
        raise agent_result
    if isinstance(agent_result, DeviceBindException):
        raise agent_result

    private_config = agent_result if not isinstance(agent_result, Exception) else {}
    if correct_words:
        private_config["correct_words"] = correct_words
    return private_config


def ensure_directories(config):
    """确保所有配置路径存在"""
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # 根据selected_module创建模型目录
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

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
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
