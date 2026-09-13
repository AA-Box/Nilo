import asyncio
import os

from config.config_loader import custom_config_path, load_config, read_config

config_file_valid = False


def check_config_file():
    """Fail fast with a clear message when the user config file is missing or mixed up."""
    global config_file_valid
    if config_file_valid:
        return
    custom_path = custom_config_path()
    if not os.path.exists(custom_path):
        raise FileNotFoundError(
            f"User config file not found: {custom_path}. Create it (an empty file is fine) "
            "or point NILO_CONFIG at your config file. See docs/getting-started.md."
        )

    config = asyncio.run(load_config())
    if config.get("read_config_from_api", False):
        old_config_origin = read_config(custom_path) or {}
        if old_config_origin.get("selected_module") is not None:
            raise ValueError(
                "The user config contains both remote-config settings (manager-api) and local "
                "module selection (selected_module). Use one or the other."
            )
    config_file_valid = True
