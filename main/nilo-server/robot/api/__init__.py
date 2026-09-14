"""The robot admin API: inspect memory, and ask why the robot is doing what it is doing.

    from robot.api import build_app, run_api

Its own aiohttp app, on its own port, with its own credential — never routes bolted onto
the device HTTP server (docs/robot-architecture.md §4.2). aiohttp is imported lazily
inside :func:`build_app`, so importing this package costs nothing and the dev test slice
runs without aiohttp installed.

There is no endpoint here that moves a robot. Actuation goes through the action layer and
its safety policy; an admin surface that could drive a robot would be a second path to the
hardware, and this is not one.
"""

from robot.api.server import (
    ADMIN_TOKEN_ENV,
    DEFAULT_API_PORT,
    ApiError,
    MemorySource,
    admin_token_from_env,
    build_app,
    run_api,
    supervise,
)

__all__ = [
    "ADMIN_TOKEN_ENV",
    "DEFAULT_API_PORT",
    "ApiError",
    "MemorySource",
    "admin_token_from_env",
    "build_app",
    "run_api",
    "supervise",
]
