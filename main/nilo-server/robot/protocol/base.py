"""Protocol specs and the registry that turns config into routes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class ProtocolSpec(BaseModel):
    """One named device-facing protocol and the HTTP/WebSocket routes it owns."""

    model_config = ConfigDict(frozen=True)

    name: str
    ws_path: str
    ota_path: str
    enabled: bool = True

    @field_validator("ws_path", "ota_path")
    @classmethod
    def _slashed(cls, value: str) -> str:
        if not value.startswith("/") or not value.endswith("/"):
            raise ValueError("protocol paths must start and end with '/'")
        return value

    @property
    def ota_download_path(self) -> str:
        """aiohttp route pattern for firmware downloads under this protocol."""
        return f"{self.ota_path}download/{{filename}}"


def _normalize(path: str) -> str:
    # ignore query string and a missing trailing slash: "/nilo/v1?x=1" == "/nilo/v1/"
    return path.split("?", 1)[0].rstrip("/") or "/"


class ProtocolRegistry:
    """Enabled/disabled view over the known protocols.

    ``strict`` controls what happens to a WebSocket path that matches *no*
    known protocol. It defaults to rejecting them: the inherited server accepted
    any path, which would let a device keep connecting on a route this server no
    longer serves. Set ``protocols.strict: false`` to restore the old behaviour.
    """

    def __init__(self, specs: Sequence[ProtocolSpec], strict: bool = True) -> None:
        if not specs:
            raise ValueError("at least one protocol spec is required")
        self._specs = tuple(specs)
        self.strict = strict

    @classmethod
    def from_config(cls, config: Mapping[str, Any], specs: Sequence[ProtocolSpec]) -> ProtocolRegistry:
        block = config.get("protocols") or {}
        if not isinstance(block, Mapping):
            raise ValueError("config 'protocols' must be a mapping")
        resolved = []
        for spec in specs:
            entry = block.get(spec.name) or {}
            enabled = bool(entry.get("enabled", True)) if isinstance(entry, Mapping) else bool(entry)
            resolved.append(spec.model_copy(update={"enabled": enabled}))
        return cls(resolved, strict=bool(block.get("strict", True)))

    @property
    def specs(self) -> tuple[ProtocolSpec, ...]:
        return self._specs

    def enabled(self) -> list[ProtocolSpec]:
        return [s for s in self._specs if s.enabled]

    def default(self) -> ProtocolSpec:
        """The protocol advertised to devices that have not chosen one (OTA)."""
        for spec in self._specs:
            if spec.enabled:
                return spec
        raise RuntimeError("no device protocol is enabled; check the 'protocols' config block")

    def match_ws_path(self, path: str) -> ProtocolSpec | None:
        wanted = _normalize(path)
        for spec in self._specs:
            if _normalize(spec.ws_path) == wanted:
                return spec
        return None

    def ws_accepts(self, path: str) -> bool:
        spec = self.match_ws_path(path)
        if spec is not None:
            return spec.enabled
        return not self.strict

    def ws_url(self, host: str, port: int) -> str:
        return f"ws://{host}:{port}{self.default().ws_path}"
