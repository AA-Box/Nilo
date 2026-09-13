# Handle the opus shared library before importing opuslib
import ctypes
import ctypes.util
import os
import platform
import sys
from enum import Enum
from pathlib import Path
from typing import cast

from config.logger import setup_logging

APP_DIR = str(Path(__file__).resolve().parent.parent)

logger = setup_logging()


class Platform(Enum):
    WINDOWS = "windows"
    MACOS = "darwin"
    LINUX = "linux"


class Arch(Enum):
    WINDOWS = {"arm": "arm64", "intel": "x64"}
    MACOS = {"arm": "arm64", "intel": "x64"}
    LINUX = {"arm": "arm64", "intel": "x64"}


class OpusInfo(Enum):
    WINDOWS = {"name": "opus.dll", "system_name": ["opus"]}
    MACOS = {"name": "libopus.dylib", "system_name": ["libopus.dylib"]}
    LINUX = {"name": "libopus.so", "system_name": ["libopus.so.0", "libopus.so"]}


def _get_platform_dict() -> dict[Platform, dict]:
    """Get the platform mapping dict"""
    return {
        Platform.WINDOWS: {
            "arch": Arch.WINDOWS,
            "lib_info": OpusInfo.WINDOWS,
            "dir": "win",
        },
        Platform.MACOS: {
            "arch": Arch.MACOS,
            "lib_info": OpusInfo.MACOS,
            "dir": "mac",
        },
        Platform.LINUX: {
            "arch": Arch.LINUX,
            "lib_info": OpusInfo.LINUX,
            "dir": "linux",
        },
    }


def get_platform() -> Platform:
    """Get the current platform"""
    system = platform.system().lower()
    if system in ("windows", "win32", "cygwin"):
        return Platform.WINDOWS
    if system == "darwin":
        return Platform.MACOS
    return Platform.LINUX


def get_arch(system: Platform) -> tuple[str, str]:
    """Get the current architecture

    Args:
        system: platform enum value

    Returns:
        (raw architecture string, normalised architecture name)
    """
    architecture = platform.machine().lower()
    is_arm = "arm" in architecture or "aarch64" in architecture

    platform_dict = _get_platform_dict()
    arch_map = platform_dict[system]["arch"].value
    arch_name = arch_map["arm" if is_arm else "intel"]

    return architecture, arch_name


def get_lib_name(system: Platform, local: bool = True) -> str | list[str]:
    """Get the Opus library name for the platform/architecture"""
    key = "name" if local else "system_name"
    platform_dict = _get_platform_dict()
    return platform_dict[system]["lib_info"].value[key]


def get_system_info() -> tuple[Platform, str]:
    """Get current system info

    Returns:
        (platform, architecture name)
    """
    system = get_platform()
    _, arch_name = get_arch(system)
    logger.info(f"Detected platform/architecture: {system.value} {arch_name}")
    return system, arch_name


def _build_lib_candidates(
    base_libs_path: Path, system_dir: str, arch_name: str
) -> list[Path]:
    """Build the list of candidate library directories (ordered by priority)

    Args:
        base_libs_path: base libs directory path
        system_dir: system directory name (e.g. win, mac, linux)
        arch_name: architecture name (e.g. x64, arm64)

    Returns:
        list of candidate paths (ordered by priority)
    """
    candidates = []

    # Priority 1: platform- and architecture-specific directory
    specific_path = base_libs_path / system_dir / arch_name
    if specific_path.is_dir():
        candidates.append(specific_path)

    # Priority 2: platform-specific directory
    platform_path = base_libs_path / system_dir
    if platform_path.is_dir() and platform_path not in candidates:
        candidates.append(platform_path)

    # Priority 3: base libs directory
    if base_libs_path.is_dir() and base_libs_path not in candidates:
        candidates.append(base_libs_path)

    return candidates


def get_search_paths(system: Platform, arch_name: str) -> list[tuple[str, str]]:
    """Get the list of library search paths

    Priority: platform/architecture-specific > platform-specific > generic > project root

    Args:
        system: platform enum value
        arch_name: architecture name

    Returns:
        list of (directory path, file name) tuples
    """
    lib_name = cast(str, get_lib_name(system))
    search_paths: list[tuple[str, str]] = []

    platform_dict = _get_platform_dict()
    system_dir = platform_dict[system]["dir"]
    base_libs_path = Path(APP_DIR) / "libs"

    # If the libs directory does not exist, return the project root directly
    if not base_libs_path.is_dir():
        logger.debug(f"libs directory not found: {base_libs_path}")
        return [(APP_DIR, lib_name)]

    # Candidate path list
    lib_candidates = _build_lib_candidates(base_libs_path, system_dir, arch_name)
    for lib_path in lib_candidates:
        search_paths.append((str(lib_path), lib_name))
        logger.debug(f"Found libs directory: {lib_path}")

    # Add the project root as the last fallback
    if not search_paths or APP_DIR not in [s[0] for s in search_paths]:
        search_paths.append((APP_DIR, lib_name))

    # Debug log: show all search paths
    for dir_path, filename in search_paths:
        full_path = os.path.join(dir_path, filename)
        exists = os.path.exists(full_path)
        logger.debug(f"Search path: {full_path} (exists: {exists})")

    return search_paths


def find_system_opus(system: Platform) -> str:
    """Look for the Opus library on the system path

    Args:
        system: platform enum value

    Returns:
        path of the library found, or an empty string if not found
    """
    lib_names = get_lib_name(system, local=False)

    if isinstance(lib_names, str):
        lib_names = [lib_names]

    for lib_name in lib_names:
        try:
            system_lib_path = ctypes.util.find_library(lib_name)
            if system_lib_path:
                logger.info(f"Found Opus library on system path: {system_lib_path}")
                return system_lib_path

            # Try loading by library name directly
            _ = ctypes.cdll.LoadLibrary(lib_name)
            logger.info(f"Loaded system Opus library directly: {lib_name}")
            return lib_name

        except (OSError, TypeError) as e:
            logger.debug(f"Failed to load system library: {lib_name} - {e}")
            continue

    logger.debug("Opus library not found on the system")
    return ""


def _find_local_opus(system: Platform, arch_name: str) -> str | None:
    """Look for the Opus library in the local search paths

    Args:
        system: platform enum value
        arch_name: architecture name

    Returns:
        path of the library file found, or None if not found
    """
    search_paths = get_search_paths(system, arch_name)

    for dir_path, file_name in search_paths:
        full_path = os.path.join(dir_path, file_name)
        if os.path.exists(full_path):
            return str(full_path)
    return None


def _setup_dll_search_path(system: Platform, lib_dir: str) -> None:
    """Set up the DLL search path on Windows

    Args:
        system: platform enum value
        lib_dir: directory containing the library file
    """
    if system != Platform.WINDOWS or not lib_dir:
        return

    try:
        if hasattr(os, "add_dll_directory"):
            dll_dir_handle = os.add_dll_directory(lib_dir)
            setattr(sys, "_opus_dll_dir_handle", dll_dir_handle)
            logger.debug(f"Added DLL search path: {lib_dir}")
    except OSError as e:
        logger.warning(f"Failed to add DLL search path: {e}")

    os.environ["PATH"] = lib_dir + os.pathsep + os.environ.get("PATH", "")


def _patch_find_library(lib_name: str, lib_path: str) -> None:
    """Patch ctypes.util.find_library so opuslib_next can find the opus library

    Args:
        lib_name: library name
        lib_path: library file path
    """
    original_find_library = ctypes.util.find_library

    def patched_find_library(name: str) -> str | None:
        if name == lib_name:
            return lib_path
        return original_find_library(name)

    ctypes.util.find_library = patched_find_library


def _load_opus_library(lib_path: str) -> bool:
    """Try to load the opus library"""
    try:
        # Load the library and keep the handle on sys so garbage collection does not release it
        cdll_instance = ctypes.CDLL(lib_path)
        setattr(sys, "_opus_cdll", cdll_instance)
        logger.info(f"Successfully loaded Opus library: {lib_path}")
        setattr(sys, "_opus_loaded", True)
        return True
    except OSError as e:
        logger.error(f"Failed to load Opus library: {lib_path} - {e}")
        return False


def setup_opus() -> bool:
    """Load the Opus shared library - priority: system library > local library

    Returns:
        True if loaded successfully, otherwise False
    """
    # Check whether the Opus library is already loaded in this process to avoid re-initialising
    if hasattr(sys, "_opus_loaded"):
        logger.info("Opus library already loaded, skipping re-initialisation")

    system, arch_name = get_system_info()
    final_lib_path = ""

    logger.info("Trying to load Opus library from system path")
    system_lib_path = find_system_opus(system)

    if system_lib_path:
        # 1. Try loading from the system path
        logger.info(f"Found Opus library on the system: {system_lib_path}")
        final_lib_path = system_lib_path
    else:
        # 2. Try the local search paths
        logger.info("Not found on system path, trying to load Opus library locally")
        local_lib_path = _find_local_opus(system, arch_name)

        if local_lib_path:
            lib_dir = str(Path(local_lib_path).parent)
            _setup_dll_search_path(system, lib_dir)
            final_lib_path = local_lib_path
        else:
            logger.debug("No local Opus library file found")

    # Patch so opuslib_next finds the correct library path
    if final_lib_path:
        loaded = _load_opus_library(final_lib_path)
        if loaded:
            _patch_find_library("opus", final_lib_path)
        return loaded

    logger.error("Unable to load Opus library")
    return False
