"""Who may call the management API, from where, and how often.

    security = ApiSecurity.from_env(host="127.0.0.1")
    security.authorize(request, Sensitivity.CONTROL)

Three independent gates, because the management API is the first surface in this project
that can *move a robot* and one gate is not enough for that:

1. **A token**, compared in constant time, distinct from the device-token signing key. The
   OTA endpoint is unauthenticated and will mint a valid device token for whatever device
   id the caller asks for (``core/api/ota_handler.py``), so "the caller holds a valid
   device token" authorizes nothing here (docs/safety-model.md).
2. **A bind gate.** Control endpoints are refused outright when the API is listening on
   anything but loopback, unless a deployment has *explicitly* said otherwise. A token
   leaked into a shell history is a token; a robot on a network with no second gate is a
   robot somebody else can drive.
3. **A rate limit**, on control endpoints only. A held-down arrow key in a dashboard, a
   retry loop, or a script with a typo should not be able to queue two hundred moves.

Read-only endpoints pass gates 1 and 3 trivially and are unaffected by gate 2: reading a
battery percentage from another host is not the thing that needs protecting.

None of this is the safety policy. Everything authorized here still goes through the
action layer, which is where a command is judged against the world
(docs/robot-architecture.md Sect. 3).
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from ipaddress import ip_address
from typing import Any

logger = logging.getLogger(__name__)

#: Environment variable the admin token is read from. Not the server config dict: in
#: manager-api mode the local configuration is replaced wholesale by the API response
#: (``config/config_loader.py``), and a credential that vanishes in the deployment with
#: the most robots in it is not a credential.
ADMIN_TOKEN_ENV = "NILO_ROBOT_ADMIN_TOKEN"
#: A file holding the admin token, for a deployment that mounts secrets rather than
#: exporting them. Read first, because that is the direction a hardened deployment moves
#: in: `docker compose` and every orchestrator can mount a file, and a file does not show
#: up in `ps`, in an image layer or in a crash dump of the environment.
ADMIN_TOKEN_FILE_ENV = "NILO_ROBOT_ADMIN_TOKEN_FILE"

#: Set to ``1``/``true``/``yes`` to allow control endpoints when the API is not on
#: loopback. Deliberately a separate switch from the host: binding to ``0.0.0.0`` to read
#: telemetry from a laptop is a reasonable thing to want, and it should not silently also
#: publish the endpoint that drives the robot across the room.
ALLOW_REMOTE_CONTROL_ENV = "NILO_ROBOT_API_ALLOW_REMOTE_CONTROL"

#: Host and port the management API binds to when nothing says otherwise.
API_HOST_ENV = "NILO_ROBOT_API_HOST"
API_PORT_ENV = "NILO_ROBOT_API_PORT"

#: The port the management API listens on by default. Not the device port.
DEFAULT_API_PORT = 8010

#: Control requests allowed per client, per window.
DEFAULT_RATE_LIMIT = 30
DEFAULT_RATE_WINDOW_S = 60.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class Sensitivity(str, Enum):
    """What class of endpoint is being called.

    ``PUBLIC``   liveness only. No token, no robot data.
    ``READ``     inspection. Token required.
    ``CONTROL``  it moves a robot, changes autonomy, or injects a sensor reading.
    """

    PUBLIC = "public"
    READ = "read"
    CONTROL = "control"


class ApiError(Exception):
    """An error with an HTTP status. Turned into a JSON body by the handler wrapper."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


#: How many clients the rate limiter remembers at once. Well above any real deployment —
#: the management API is one dashboard and one scraper — and a ceiling rather than a rule.
MAX_TRACKED_CLIENTS = 1024


class RateLimiter:
    """A fixed-window counter per client. Small, in-process, and good enough for a control plane.

    Not a token bucket and not distributed: this guards one process's control endpoints
    against a stuck key and a retry loop, not a determined attacker who already has the
    token. The failure mode that matters is a dashboard queueing two hundred moves.
    """

    def __init__(
        self,
        limit: int = DEFAULT_RATE_LIMIT,
        window_s: float = DEFAULT_RATE_WINDOW_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, client: str) -> None:
        """Record one request. Raises :class:`ApiError` 429 when the window is full."""
        if self.limit <= 0:
            return
        now = self._clock()
        self._evict(now)
        hits = self._hits.setdefault(client, deque())
        cutoff = now - self.window_s
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            retry_after = max(1, int(round(self.window_s - (now - hits[0]))))
            raise ApiError(
                429,
                f"too many control requests: at most {self.limit} every "
                f"{self.window_s:.0f}s. Try again in {retry_after}s.",
            )
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()

    def _evict(self, now: float) -> None:
        """Forget clients with nothing in the window, and cap how many are tracked.

        The map is keyed on a peer address, so without this it grows for the life of the
        process — one entry per address that ever made a control request. Only an
        authenticated caller can reach it (the token is checked first), which makes this a
        slow leak rather than a denial of service, and a slow leak in a process meant to
        run for months is still a leak.
        """
        if len(self._hits) < MAX_TRACKED_CLIENTS:
            return
        cutoff = now - self.window_s
        for client in [key for key, hits in self._hits.items() if not hits or hits[-1] < cutoff]:
            del self._hits[client]
        if len(self._hits) >= MAX_TRACKED_CLIENTS:
            # Every tracked client is active. Keep the newest, and let the rest through:
            # a rate limiter that runs out of memory is worse than one that forgets.
            newest = sorted(self._hits.items(), key=lambda entry: entry[1][-1], reverse=True)
            self._hits = dict(newest[: MAX_TRACKED_CLIENTS // 2])


def is_loopback(host: str) -> bool:
    """Whether a bind address only accepts connections from this machine."""
    cleaned = (host or "").strip().strip("[]")
    if cleaned in {"localhost", ""}:
        return True
    try:
        return ip_address(cleaned).is_loopback
    except ValueError:
        return False


def admin_token_from_env(env: dict[str, str] | None = None) -> str | None:
    """The admin token from the environment, or ``None`` if it is not set.

    ``None`` means the API refuses every authenticated route — it fails closed, which is
    the right behaviour for a surface that can delete a robot's memory and move it.
    """
    source = os.environ if env is None else env
    from robot.config import resolve_secret  # noqa: PLC0415 - one direction, and only here

    token = resolve_secret(
        source.get(ADMIN_TOKEN_ENV, ""), file_path=source.get(ADMIN_TOKEN_FILE_ENV, "") or None
    ).strip()
    if not token:
        logger.warning(
            "neither %s nor %s is set; the robot management API will refuse every "
            "authenticated request",
            ADMIN_TOKEN_ENV,
            ADMIN_TOKEN_FILE_ENV,
        )
        return None
    return token


def _flag(name: str, env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(name, "").strip().lower() in _TRUTHY


@dataclass
class ApiSecurity:
    """The three gates, as one object a handler asks a question of."""

    token: str | None = None
    host: str = "127.0.0.1"
    allow_remote_control: bool = False
    require_auth: bool = True
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)

    @classmethod
    def from_env(
        cls,
        *,
        host: str | None = None,
        env: dict[str, str] | None = None,
        require_auth: bool = True,
        rate_limiter: RateLimiter | None = None,
    ) -> ApiSecurity:
        source = os.environ if env is None else env
        return cls(
            token=admin_token_from_env(env),
            host=host if host is not None else source.get(API_HOST_ENV, "127.0.0.1"),
            allow_remote_control=_flag(ALLOW_REMOTE_CONTROL_ENV, env),
            require_auth=require_auth,
            rate_limiter=rate_limiter or RateLimiter(),
        )

    @property
    def local_only(self) -> bool:
        return is_loopback(self.host)

    @property
    def control_enabled(self) -> bool:
        """Whether control endpoints will answer at all on this binding."""
        return self.local_only or self.allow_remote_control

    def describe(self) -> dict[str, Any]:
        """What the dashboard shows in its corner, and what the OpenAPI document says."""
        return {
            "host": self.host,
            "local_only": self.local_only,
            "control_enabled": self.control_enabled,
            "authentication": "bearer" if self.require_auth else "disabled",
            "rate_limit": {"requests": self.rate_limiter.limit, "window_s": self.rate_limiter.window_s},
        }

    def warn_if_open(self) -> None:
        """Log loudly at startup when this binding is wider than loopback."""
        if self.local_only:
            return
        if self.allow_remote_control:
            logger.warning(
                "the robot management API is bound to %s with control endpoints ENABLED (%s is set). "
                "Anything that reaches this port and holds the admin token can drive the robot.",
                self.host,
                ALLOW_REMOTE_CONTROL_ENV,
            )
        else:
            logger.info(
                "the robot management API is bound to %s; control endpoints are refused because "
                "%s is not set",
                self.host,
                ALLOW_REMOTE_CONTROL_ENV,
            )

    # -- the gates ------------------------------------------------------------------------------

    def authorize(self, request: Any, sensitivity: Sensitivity = Sensitivity.READ) -> None:
        """Apply every gate this sensitivity needs. Raises :class:`ApiError`."""
        if sensitivity is Sensitivity.PUBLIC:
            return
        self._check_token(request)
        if sensitivity is not Sensitivity.CONTROL:
            return
        if not self.control_enabled:
            raise ApiError(
                403,
                "control endpoints are disabled: the management API is not on loopback and "
                f"{ALLOW_REMOTE_CONTROL_ENV} is not set",
            )
        self.rate_limiter.check(client_key(request))

    def _check_token(self, request: Any) -> None:
        if not self.require_auth:
            return
        if not self.token:
            raise ApiError(503, "the robot management API has no token configured")
        header = str(getattr(request, "headers", {}).get("Authorization", ""))
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # Constant-time: a token check that leaks its comparison time is a token check
        # somebody can walk one character at a time.
        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise ApiError(401, "a valid admin token is required")


def client_key(request: Any) -> str:
    """Who a rate-limit window belongs to. The peer address, or ``unknown``."""
    remote = getattr(request, "remote", None)
    return str(remote) if remote else "unknown"


__all__ = [
    "ADMIN_TOKEN_ENV",
    "ADMIN_TOKEN_FILE_ENV",
    "ALLOW_REMOTE_CONTROL_ENV",
    "API_HOST_ENV",
    "API_PORT_ENV",
    "DEFAULT_API_PORT",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_WINDOW_S",
    "ApiError",
    "MAX_TRACKED_CLIENTS",
    "ApiSecurity",
    "RateLimiter",
    "Sensitivity",
    "admin_token_from_env",
    "client_key",
    "is_loopback",
]
