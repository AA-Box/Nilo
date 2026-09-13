"""NILO_* environment overrides, deprecated key aliases, NILO_CONFIG path."""
import asyncio

from config import config_loader
from config.config_loader import apply_deprecated_aliases, apply_env_overrides, custom_config_path


def test_env_overrides_win_over_config():
    cfg = {"server": {"ip": "0.0.0.0", "port": 8000, "http_port": 8003}, "log": {"log_level": "INFO"}}
    env = {"NILO_SERVER_HOST": "127.0.0.1", "NILO_SERVER_PORT": "9000", "NILO_HTTP_PORT": "9003", "NILO_LOG_LEVEL": "DEBUG"}
    apply_env_overrides(cfg, env)
    assert cfg["server"] == {"ip": "127.0.0.1", "port": 9000, "http_port": 9003}
    assert cfg["log"]["log_level"] == "DEBUG"


def test_env_overrides_ignore_unset_and_empty():
    cfg = {"server": {"port": 8000}}
    apply_env_overrides(cfg, {"NILO_SERVER_PORT": ""})
    assert cfg["server"]["port"] == 8000


def test_env_overrides_create_missing_section():
    cfg = {}
    apply_env_overrides(cfg, {"NILO_LOG_LEVEL": "WARNING"})
    assert cfg == {"log": {"log_level": "WARNING"}}


def test_no_deprecated_aliases_remain():
    """The inherited `xiaozhi:` alias was removed with the legacy protocol."""
    assert config_loader.DEPRECATED_KEYS == {}
    cfg = {"xiaozhi": {"version": 1}}
    apply_deprecated_aliases(cfg)
    assert cfg == {"xiaozhi": {"version": 1}}  # left alone; it is simply an unknown key now


def test_nilo_config_env_selects_override_file(monkeypatch, tmp_path):
    f = tmp_path / "mine.yaml"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NILO_CONFIG", str(f))
    assert custom_config_path() == str(f)
    monkeypatch.delenv("NILO_CONFIG")
    assert custom_config_path().endswith("data/.config.yaml")


def test_load_config_applies_alias_and_env(monkeypatch, tmp_path):
    from core.utils.cache.manager import cache_manager, CacheType

    f = tmp_path / "override.yaml"
    f.write_text("hello:\n  version: 42\n", encoding="utf-8")
    monkeypatch.setenv("NILO_CONFIG", str(f))
    monkeypatch.setenv("NILO_SERVER_PORT", "8123")
    cache_manager.delete(CacheType.CONFIG, "main_config")
    try:
        cfg = asyncio.run(config_loader.load_config())
    finally:
        cache_manager.delete(CacheType.CONFIG, "main_config")
    assert cfg["hello"]["version"] == 42  # user override merged into the shipped hello block
    assert cfg["hello"]["type"] == "hello"
    assert cfg["server"]["port"] == 8123
    assert cfg["protocols"]["nilo"]["enabled"] is True
    assert cfg["protocols"]["strict"] is True
