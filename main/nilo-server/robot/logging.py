"""Logging for the robot subsystem: stdlib loggers, bridged into the server's sink.

Robot modules use plain ``logging.getLogger(__name__)``. They must not touch
``config/logger.py:setup_logging`` — it reads the config file and configures loguru
globally, and ``robot/`` has to stay importable in a process with no config file at all
(docs/robot-architecture.md R12).

That leaves one gap: stdlib records go nowhere useful in a server whose sinks are loguru,
so ``INFO`` from the robot subsystem would be invisible in production. :func:`install`
closes it, and is called once from ``app.py`` after logging is configured.
"""

from __future__ import annotations

import logging
from types import FrameType
from typing import Any

ROOT_LOGGER_NAME = "robot"


class LoguruBridgeHandler(logging.Handler):
    """Forwards stdlib records to loguru, tagged with the module that emitted them."""

    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger

        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        bound: Any = logger.opt(depth=depth, exception=record.exc_info).bind(tag=record.name)
        bound.log(level, record.getMessage())


def install(level: int | str = logging.INFO) -> logging.Logger:
    """Route ``robot.*`` stdlib logging into loguru. Idempotent; safe to call at startup."""
    robot_logger = logging.getLogger(ROOT_LOGGER_NAME)
    if not any(isinstance(handler, LoguruBridgeHandler) for handler in robot_logger.handlers):
        robot_logger.addHandler(LoguruBridgeHandler())
    robot_logger.setLevel(level)
    robot_logger.propagate = False
    return robot_logger
