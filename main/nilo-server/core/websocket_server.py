import asyncio
import logging

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """Filter out invalid-handshake error logs (e.g. HTTPS hitting the WS port)."""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """Attach the invalid-handshake filter to every websockets logger."""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api_async
from core.auth import AuthManager, AuthenticationError
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update
from robot.protocol import registry_from_config

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.config_lock = asyncio.Lock()
        self.protocols = registry_from_config(config)
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # Device allowlist
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        async with websockets.serve(
            self._handle_connection,
            host,
            port,
            process_request=self._http_response,
            ping_interval=None,
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # Fall back to the device-id from the URL query string
            from urllib.parse import parse_qs, urlparse

            # Path of the WebSocket request
            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("WebSocket request has no path")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send(
                    "nilo-server is up. Connect with a device-id header or ?device-id= query parameter."
                )
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """Handle a new connection with its own ConnectionHandler."""
        # Authenticate before establishing the connection
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("authentication failed")
            await websocket.close()
            return
        # Pass this server instance to the ConnectionHandler
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # server instance
        )

        # Plugins are registered in connection.py's handle_connection via register_plugins_to_conn
        # No need to register them again here; kept for backward compatibility
        
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Error handling connection: {e}")
        finally:
            # Force-close the connection if it is still open
            try:
                # Check the WebSocket state safely before closing
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # No closed attribute; just try to close
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"Error force-closing connection on server side: {close_error}"
                )

    async def _http_response(self, websocket, request):
        """websockets process_request hook: gate WebSocket paths, answer plain HTTP probes."""
        connection_header = request.headers.get("connection", "").lower()
        if "upgrade" in connection_header:
            if self.protocols.ws_accepts(request.path):
                return None  # let the handshake continue
            self.logger.bind(tag=TAG).warning(f"rejected WebSocket path {request.path!r}")
            return websocket.respond(404, "unknown protocol path\n")
        return websocket.respond(200, "nilo-server is running\n")

    async def update_config(self) -> bool:
        """Refresh the server config and re-initialize components.

        Returns:
            bool: whether the update succeeded
        """
        try:
            async with self.config_lock:
                # Re-fetch the config (async version)
                new_config = await get_config_from_api_async(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("Failed to fetch new config")
                    return False
                self.logger.bind(tag=TAG).info(f"Fetched new config")
                # Check whether the VAD and ASR types need updating
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"VAD/ASR update needed: {update_vad} {update_asr}"
                )
                # Update config
                self.config = new_config
                self.protocols = registry_from_config(new_config)
                # Re-initialize components
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    False,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # Update component instances
                if "vad" in modules:
                    self._vad = modules["vad"]
                if "asr" in modules:
                    self._asr = modules["asr"]
                if "llm" in modules:
                    self._llm = modules["llm"]
                if "intent" in modules:
                    self._intent = modules["intent"]
                if "memory" in modules:
                    self._memory = modules["memory"]
                self.logger.bind(tag=TAG).info(f"Config update task completed")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to update server config: {str(e)}")
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        # Authenticate before establishing the connection
        if self.auth_enable:
            headers = dict(websocket.request.headers)
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                # Allowlisted devices skip token verification
                return
            else:
                # Otherwise verify the token
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    token = token[7:]  # Strip the 'Bearer ' prefix
                else:
                    raise AuthenticationError("Missing or invalid Authorization header")
                # Authenticate
                auth_success = self.auth.verify_token(
                    token, client_id=client_id, username=device_id
                )
                if not auth_success:
                    raise AuthenticationError("Invalid token")
