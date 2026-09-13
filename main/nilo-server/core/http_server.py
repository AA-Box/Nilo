"""HTTP side of nilo-server: OTA bootstrap per protocol, vision explain endpoint."""
import asyncio
from aiohttp import web
from config.logger import setup_logging
from config.placeholders import is_placeholder
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from robot.protocol import registry_from_config

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.protocols = registry_from_config(config)
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """WebSocket URL advertised to devices: server.websocket if set, else the default protocol route."""
        websocket_config = self.config["server"].get("websocket")
        if websocket_config and not is_placeholder(websocket_config):
            return websocket_config
        return self.protocols.ws_url(local_ip, port)

    def _build_app(self) -> web.Application:
        """Route table. Kept separate from start() so tests can inspect it without binding a port."""
        app = web.Application()
        read_config_from_api = self.config.get("read_config_from_api", False)

        if not read_config_from_api:
            # Standalone mode: serve the OTA bootstrap that hands devices their WebSocket URL,
            # once per enabled protocol, plus firmware download from data/bin.
            for spec in self.protocols.enabled():
                app.add_routes(
                    [
                        web.get(spec.ota_path, self.ota_handler.handle_get),
                        web.post(spec.ota_path, self.ota_handler.handle_post),
                        web.options(spec.ota_path, self.ota_handler.handle_options),
                        web.get(spec.ota_download_path, self.ota_handler.handle_download),
                        web.options(spec.ota_download_path, self.ota_handler.handle_options),
                    ]
                )

        app.add_routes(
            [
                web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                web.post("/mcp/vision/explain", self.vision_handler.handle_post),
                web.options("/mcp/vision/explain", self.vision_handler.handle_options),
            ]
        )
        return app

    async def start(self):
        try:
            server_config = self.config["server"]
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = self._build_app()

                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                while True:
                    await asyncio.sleep(3600)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP server failed to start: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"traceback: {traceback.format_exc()}")
            raise
