"""The robot management API and the development dashboard.

    from robot.api import build_app, run_api

    app = build_app(runtime, memories, token="…")       # aiohttp application
    await run_api(runtime, memories, token="…", port=8010)

Its own aiohttp app, on its own port, with its own credential — never routes bolted onto
the device HTTP server (docs/robot-architecture.md §4.2). aiohttp is imported lazily
inside :func:`build_app`, so importing this package costs nothing and the dev test slice
runs without aiohttp installed.

``security``   the three gates: a token, a loopback rule, a rate limit
``views``      domain objects as JSON, in one place
``control``    the endpoints that move a robot, and what stands in front of them
``simulate``   injected perception, through the same doors real perception uses
``events``     the live server-sent event stream
``openapi``    the document, generated from the request models
``dashboard``  one HTML file, no build step
``server``     the assembly

This API **can** move a robot, which is a change from the phase in which it deliberately
could not. What has not changed is where the decision is made: a control request becomes a
typed action submitted to the same executor and judged by the same safety policy as a
behaviour's own command. There is no privileged path, and a stop this API sends is a
request the firmware watchdog backs rather than a guarantee (docs/safety.md).
"""

from robot.api.security import (
    ADMIN_TOKEN_ENV,
    ALLOW_REMOTE_CONTROL_ENV,
    API_HOST_ENV,
    API_PORT_ENV,
    DEFAULT_API_PORT,
    DEFAULT_RATE_LIMIT,
    DEFAULT_RATE_WINDOW_S,
    ApiError,
    ApiSecurity,
    RateLimiter,
    Sensitivity,
    admin_token_from_env,
    is_loopback,
)
from robot.api.server import MemorySource, build_app, run_api, supervise

__all__ = [
    "ADMIN_TOKEN_ENV",
    "ALLOW_REMOTE_CONTROL_ENV",
    "API_HOST_ENV",
    "API_PORT_ENV",
    "DEFAULT_API_PORT",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_WINDOW_S",
    "ApiError",
    "ApiSecurity",
    "MemorySource",
    "RateLimiter",
    "Sensitivity",
    "admin_token_from_env",
    "build_app",
    "is_loopback",
    "run_api",
    "supervise",
]
