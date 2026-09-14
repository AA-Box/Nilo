"""The configuration hierarchy, and the composition root that turns it into a subsystem."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from robot.behavior.base import AutonomyMode
from robot.bootstrap import build_runtime, start_robot_subsystem
from robot.config import CONFIG_PATH_ENV, RobotConfig, resolve_secret
from robot.runtime import set_runtime


@pytest.fixture(autouse=True)
def in_a_clean_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every loader's default path is relative to the working directory. Give it a fresh one."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    set_runtime(None)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- the defaults ---------------------------------------------------------------------------------


def test_no_file_anywhere_is_conservative_defaults_rather_than_an_error() -> None:
    settings = RobotConfig.load(environ={})
    assert settings.safety.max_speed_mmps == 300
    assert settings.memory.enabled is False, "a runtime must not create a database by existing"
    assert settings.vision.enabled is False
    assert settings.robot.autostart_behaviors is False, "a robot must not move because it was plugged in"


def test_the_description_is_one_line_and_names_no_secret() -> None:
    line = RobotConfig.load(environ={}).describe()
    assert "\n" not in line
    assert "autonomy=" in line and "speed<=" in line


# -- the hierarchy ---------------------------------------------------------------------------------


def test_one_document_configures_every_area(tmp_path: Path) -> None:
    write(
        tmp_path / "data" / "robot.yaml",
        """
        robot:
          autonomy: full
          autostart_behaviors: true
        safety:
          max_speed_mmps: 150
          max_distance_mm: 400
        memory:
          enabled: true
          path: ":memory:"
        vision:
          enabled: true
          detector: colour-blob
        personality:
          traits:
            curiosity: 0.9
        """.replace("        ", ""),
    )
    settings = RobotConfig.load(environ={})
    assert settings.robot.autonomy == "full"
    assert settings.safety.max_speed_mmps == 150
    assert settings.memory.enabled and settings.memory.path == ":memory:"
    assert settings.vision.detector == "colour-blob"
    assert settings.personality.traits.curiosity == pytest.approx(0.9)


def test_the_config_path_variable_points_somewhere_else(tmp_path: Path) -> None:
    elsewhere = write(tmp_path / "elsewhere.yaml", "safety:\n  max_speed_mmps: 99\n")
    settings = RobotConfig.load(environ={CONFIG_PATH_ENV: str(elsewhere)})
    assert settings.safety.max_speed_mmps == 99


def test_a_legacy_per_area_file_still_configures_that_area(tmp_path: Path) -> None:
    """The migration path: a deployment that already wrote robot_limits.yaml keeps working."""
    write(tmp_path / "data" / "robot_limits.yaml", "max_speed_mmps: 120\n")
    settings = RobotConfig.load(environ={})
    assert settings.safety.max_speed_mmps == 120


def test_the_main_document_wins_over_the_legacy_file(tmp_path: Path) -> None:
    write(tmp_path / "data" / "robot_limits.yaml", "max_speed_mmps: 120\n")
    write(tmp_path / "data" / "robot.yaml", "safety:\n  max_speed_mmps: 80\n")
    assert RobotConfig.load(environ={}).safety.max_speed_mmps == 80


def test_the_environment_wins_over_the_file(tmp_path: Path) -> None:
    write(tmp_path / "data" / "robot.yaml", "safety:\n  max_speed_mmps: 80\n")
    settings = RobotConfig.load(environ={"NILO_ROBOT_SAFETY_MAX_SPEED_MMPS": "60"})
    assert settings.safety.max_speed_mmps == 60


def test_a_boolean_from_the_environment_is_a_boolean() -> None:
    for raw, expected in (("true", True), ("1", True), ("on", True), ("false", False), ("no", False)):
        settings = RobotConfig.load(environ={"NILO_ROBOT_MEMORY_ENABLED": raw})
        assert settings.memory.enabled is expected, raw


def test_a_variable_that_names_no_field_is_a_warning_not_a_silent_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure it otherwise produces is "I set the limit and nothing happened"."""
    with caplog.at_level("WARNING"):
        RobotConfig.load(environ={"NILO_ROBOT_SAFETY_MAX_VELOCITY": "5"})
    assert any("names no field" in record.getMessage() for record in caplog.records)


def test_the_api_variables_are_left_alone() -> None:
    """`NILO_ROBOT_ADMIN_TOKEN` belongs to robot/api/security.py, not to a config section."""
    settings = RobotConfig.load(
        environ={"NILO_ROBOT_ADMIN_TOKEN": "secret", "NILO_ROBOT_API_PORT": "9999"}
    )
    assert settings == RobotConfig()


def test_a_file_that_is_not_a_mapping_fails_to_start(tmp_path: Path) -> None:
    """Falling back to defaults after an operator wrote a file is worse than not starting."""
    write(tmp_path / "data" / "robot.yaml", "- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        RobotConfig.load(environ={})


def test_an_unknown_key_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    write(tmp_path / "data" / "robot.yaml", "safety:\n  max_speed_mmpss: 80\n")
    with pytest.raises(Exception, match="max_speed_mmpss|extra"):
        RobotConfig.load(environ={})


# -- secrets ---------------------------------------------------------------------------------------


def test_a_secret_comes_from_a_file_before_an_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = write(tmp_path / "token", "  from-the-file\n")
    monkeypatch.setenv("SOME_TOKEN", "from-the-environment")
    assert resolve_secret(None, file_path=str(secret), env="SOME_TOKEN") == "from-the-file"
    assert resolve_secret(None, env="SOME_TOKEN") == "from-the-environment"
    assert resolve_secret("literal") == "literal"


def test_a_missing_secret_file_is_an_empty_secret_not_an_exception(tmp_path: Path) -> None:
    """The caller decides whether empty is fatal. For the management API it is."""
    assert resolve_secret(None, file_path=str(tmp_path / "absent")) == ""


# -- the composition root ------------------------------------------------------------------------


def test_the_runtime_is_built_with_the_configured_limits() -> None:
    settings = RobotConfig.load(environ={"NILO_ROBOT_SAFETY_MAX_SPEED_MMPS": "120"})
    runtime = build_runtime(settings)
    try:
        assert runtime.actions.limits.max_speed_mmps == 120
        assert runtime.settings is settings
    finally:
        asyncio.get_event_loop_policy()  # no loop needed; the executor was never started


def test_memory_is_off_unless_it_is_asked_for(tmp_path: Path) -> None:
    runtime = build_runtime(RobotConfig.load(environ={}))
    assert asyncio.run(runtime.memory("nilo-sim-01")) is None
    assert not (tmp_path / "data").exists(), "a runtime created a database by existing"


async def test_memory_is_opened_on_first_use_when_it_is_enabled() -> None:
    settings = RobotConfig.load(
        environ={"NILO_ROBOT_MEMORY_ENABLED": "true", "NILO_ROBOT_MEMORY_PATH": ":memory:"}
    )
    runtime = build_runtime(settings)
    try:
        memory = await runtime.memory("nilo-sim-01")
        assert memory is not None
        assert await memory.counts() == {"episodes": 0, "facts": 0, "people": 0, "working": 0}
    finally:
        await runtime.aclose()


async def test_starting_the_subsystem_installs_it_and_applies_the_autonomy_mode() -> None:
    from robot.runtime import get_runtime

    settings = RobotConfig.load(environ={"NILO_ROBOT_AUTONOMY": "passive"})
    runtime = await start_robot_subsystem(settings)
    try:
        assert get_runtime() is runtime
        assert runtime.autonomy is AutonomyMode.PASSIVE
    finally:
        await runtime.aclose()


async def test_an_unknown_autonomy_mode_is_a_warning_and_the_safe_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RobotConfig.load(environ={"NILO_ROBOT_AUTONOMY": "maximum-overdrive"})
    with caplog.at_level("WARNING"):
        runtime = await start_robot_subsystem(settings)
    try:
        assert runtime.autonomy is AutonomyMode.NORMAL
        assert any("unknown autonomy mode" in record.getMessage() for record in caplog.records)
    finally:
        await runtime.aclose()


async def test_an_unknown_detector_leaves_a_pipeline_that_finds_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model file that moved must not stop the server accepting sessions."""
    from robot.bootstrap import _detector

    settings = RobotConfig.load(
        environ={"NILO_ROBOT_VISION_ENABLED": "true", "NILO_ROBOT_VISION_DETECTOR": "magic"}
    )
    with caplog.at_level("WARNING"):
        detector = _detector(settings)
    assert detector.name == "null"
    assert any("unknown detector" in record.getMessage() for record in caplog.records)


# -- the admin token -------------------------------------------------------------------------------


def test_the_admin_token_can_come_from_a_mounted_file(tmp_path: Path) -> None:
    from robot.api.security import ADMIN_TOKEN_ENV, ADMIN_TOKEN_FILE_ENV, admin_token_from_env

    secret = write(tmp_path / "admin-token", "mounted-token\n")
    assert admin_token_from_env({ADMIN_TOKEN_FILE_ENV: str(secret)}) == "mounted-token"
    # A file wins: that is the direction a hardened deployment moves in.
    assert (
        admin_token_from_env({ADMIN_TOKEN_FILE_ENV: str(secret), ADMIN_TOKEN_ENV: "from-env"})
        == "mounted-token"
    )
    assert admin_token_from_env({ADMIN_TOKEN_ENV: "from-env"}) == "from-env"


def test_no_token_anywhere_still_fails_closed(tmp_path: Path) -> None:
    from robot.api.security import ADMIN_TOKEN_FILE_ENV, admin_token_from_env

    assert admin_token_from_env({}) is None
    assert admin_token_from_env({ADMIN_TOKEN_FILE_ENV: str(tmp_path / "absent")}) is None
