from enum import Enum


class PluginAction(Enum):
    """Plugin return status enum"""
    RELEASE = "release"  # Release: continue the original flow
    INTERCEPT = "intercept"  # Intercept: return the result
    CLOSE = "close"  # Close the connection and return the result


class BasePlugin:
    """Plugin base class"""

    def __init__(self, logger=None):
        self.name = "BasePlugin"
        self.description = "Base plugin class"
        self.logger = logger

    async def pre_process_text(self, conn, text):
        """Text pre-processing hook

        Returns:
            tuple: (result, action)
                - result: the processed text or a response message
                - action: a PluginAction value (RELEASE/INTERCEPT/CLOSE)
        """
        return text, PluginAction.RELEASE

    def speak(self, conn, text):
        """Send a spoken message (wraps TTS and queue handling)"""
        from core.providers.tts.dto.dto import ContentType
        from core.handle.sendAudioHandle import send_stt_message
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_stt_message(conn, text))
        except RuntimeError:
            pass

        if hasattr(conn, 'tts') and conn.tts:
            conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)

    def get_info(self):
        """Get plugin info"""
        return {"name": self.name, "description": self.description}